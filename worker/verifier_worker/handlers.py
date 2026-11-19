"""What each kind of job does, and how its outcome is recorded.

A handler's run() does the slow work and returns a function that writes the
result. The caller runs that function in the same transaction that marks the
job done, so a result is recorded exactly when the job is.
"""

import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

import psycopg
from langchain_core.language_models import BaseChatModel
from psycopg.types.json import Jsonb

from . import agent, batfish, docs, network, routing, storage
from .config import Config
from . import jobs
from .jobs import Job
from .structured import make_ask

log = logging.getLogger("worker")

Apply = Callable[[psycopg.Connection], None]

NOT_CONFIGURED = "The agent has no model access yet. Set DEEPSEEK_API_KEY in worker/.env and restart the worker."
GAVE_UP = "I used all my steps without reaching an answer. Try a narrower question."
AGENT_FAILED = "The agent could not reply because of an internal error. Try sending your message again."
PARSE_ABANDONED = "The worker stopped responding while analysing this snapshot."


def _snapshot(conn: psycopg.Connection, investigation_id: str) -> dict[str, Any]:
    row = conn.execute(
        """
        select s.name, s.content_hash, s.s3_prefix, s.files, i.intent, i.result, i.status
          from investigations i join snapshots s on s.id = i.snapshot_id
         where i.id = %s
        """,
        (investigation_id,),
    ).fetchone()
    conn.commit()
    if row is None:
        raise RuntimeError(f"investigation {investigation_id} not found")
    keys = ("name", "content_hash", "prefix", "files", "intent", "result", "status")
    return dict(zip(keys, row))


# --- parse_snapshot -------------------------------------------------------


def run_parse(config: Config, conn: psycopg.Connection, s3, job: Job) -> Apply:
    snap = _snapshot(conn, job.investigation_id)
    with tempfile.TemporaryDirectory(prefix="verifier-snapshot-") as tmp:
        outcome = batfish.analyse(
            config.batfish_host,
            config.batfish_instance,
            snap["content_hash"],
            lambda: storage.download_snapshot(s3, config.s3_bucket, snap["prefix"], snap["files"], Path(tmp)),
        )
    log.info(
        "investigation %s is %s (%s model, %d ms)",
        job.investigation_id,
        outcome["status"],
        "reused" if outcome["model_reused"] else "new",
        outcome["parse_ms"],
    )

    def apply(c: psycopg.Connection) -> None:
        c.execute(
            """
            update investigations
               set status = %(status)s, finished_at = now(), error = %(error)s,
                   model_reused = %(model_reused)s, batfish_instance = %(batfish_instance)s,
                   batfish_version = %(batfish_version)s, parse_ms = %(parse_ms)s, result = %(result)s
             where id = %(id)s
            """,
            {**outcome, "result": Jsonb(outcome["result"]), "id": job.investigation_id},
        )

    return apply


def parse_retry(conn: psycopg.Connection, job: Job) -> None:
    conn.execute("update investigations set status = 'queued' where id = %s", (job.investigation_id,))


def parse_failed(conn: psycopg.Connection, investigation_id: str, error: str) -> None:
    conn.execute(
        """
        update investigations set status = 'failed', finished_at = now(), error = %s
         where id = %s and status in ('queued', 'parsing')
        """,
        (error, investigation_id),
    )


def network_overview(bf: Any, files: dict[str, str], parsed_files: list[dict[str, Any]]) -> str:
    """The network overview for the agent's prompt, or empty text if it cannot be computed."""
    try:
        return network.render(network.overview(bf, files, parsed_files))
    except Exception:  # noqa: BLE001 - the agent works without it, only slower
        log.exception("could not compute the network overview")
        return ""


def dataplane_error(result: dict[str, Any] | None, bf: Any) -> str | None:
    """Why Batfish cannot compute routing for this snapshot, or None when it can.

    Investigations parsed before this was recorded are checked on the spot.
    """
    status = (result or {}).get("dataplane") or batfish.dataplane_status(bf)
    return None if status["ok"] else status["error"] or "no reason given"


# --- agent_reply ----------------------------------------------------------


def _agent_message(
    investigation_id: str,
    content: str,
    steps: list[dict[str, Any]] | None = None,
    failed: bool = False,
    suggestion: dict[str, str] | None = None,
    trace: dict[str, Any] | None = None,
) -> Apply:
    """trace records what ran: route (how the message was sorted), duration_ms and model."""
    trace = trace or {}

    def apply(c: psycopg.Connection) -> None:
        c.execute(
            """
            insert into messages (investigation_id, role, content, steps, failed, suggestion, route, duration_ms, model)
            values (%s, 'agent', %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                investigation_id,
                content,
                Jsonb(steps or []),
                failed,
                Jsonb(suggestion) if suggestion else None,
                trace.get("route"),
                trace.get("duration_ms"),
                trace.get("model"),
            ),
        )

    return apply


def run_agent(config: Config, conn: psycopg.Connection, s3, job: Job, model: BaseChatModel | None) -> Apply:
    if model is None:
        return _agent_message(job.investigation_id, NOT_CONFIGURED, failed=True)

    started = time.monotonic()
    snap = _snapshot(conn, job.investigation_id)
    rows = conn.execute(
        "select role, content, failed from messages where investigation_id = %s order by created_at, id",
        (job.investigation_id,),
    ).fetchall()
    conn.commit()
    history = agent.to_history([{"role": r[0], "content": r[1], "failed": r[2]} for r in rows])

    with tempfile.TemporaryDirectory(prefix="verifier-snapshot-") as tmp:
        root = storage.download_snapshot(s3, config.s3_bucket, snap["prefix"], snap["files"], Path(tmp))
        files = {
            entry["path"]: (root / "configs" / entry["path"]).read_text(encoding="utf-8", errors="replace")
            for entry in snap["files"]
        }
        # Normally the model is already there; this re-parses if the Batfish volume was reset.
        bf, _, _ = batfish.ensure_model(config.batfish_host, snap["content_hash"], lambda: root)

    index = docs.get_index(config.database_url, config.openai_api_key, config.embedding_model)
    ctx = agent.Context(
        snapshot_name=snap["name"],
        files=files,
        parsed_files=(snap["result"] or {}).get("files", []),
        intent=snap["intent"],
        batfish=bf,
        search_docs=index.search if index else None,
        dataplane_error=dataplane_error(snap["result"], bf),
        overview=network_overview(bf, files, (snap["result"] or {}).get("files", [])),
    )
    # Progress goes out on a connection of its own, so the dashboard sees it while the job runs.
    with psycopg.connect(config.database_url, autocommit=True) as side:

        def progress(update: dict[str, Any]) -> None:
            jobs.set_progress(side, job, update)

        try:
            answer, steps = agent.reply(model, ctx, history, progress)
        except agent.AgentGaveUp:
            elapsed = int((time.monotonic() - started) * 1000)
            trace = {"duration_ms": elapsed, "model": config.agent_model}
            return _agent_message(job.investigation_id, GAVE_UP, failed=True, trace=trace)
        log.info("agent replied on investigation %s after %d tool call(s)", job.investigation_id, len(steps))
        progress({"step": len(steps), "doing": "deciding whether to suggest a workflow run"})
        conversation = [{"role": r[0], "content": r[1]} for r in rows if not r[2]]
        route, suggestion = routing.sort(make_ask(model), conversation)
    log.info("message on investigation %s sorted as %s", job.investigation_id, route or "unknown")
    trace = {"route": route, "duration_ms": int((time.monotonic() - started) * 1000), "model": config.agent_model}
    return _agent_message(job.investigation_id, answer, steps, suggestion=suggestion, trace=trace)


def agent_failed(conn: psycopg.Connection, investigation_id: str) -> None:
    _agent_message(investigation_id, AGENT_FAILED, failed=True)(conn)


# --- dispatch -------------------------------------------------------------


def record_abandoned(conn: psycopg.Connection, kind: str, investigation_id: str) -> None:
    """A job's last attempt died without reporting back: show that to the user."""
    if kind == "parse_snapshot":
        parse_failed(conn, investigation_id, PARSE_ABANDONED)
    elif kind == "agent_reply":
        agent_failed(conn, investigation_id)
    elif kind == "run_workflow":
        from . import runs  # imported here because runs builds on this module

        runs.abandoned(conn, investigation_id)
    elif kind == "deploy_change":
        from . import deploy

        deploy.abandoned(conn, investigation_id)
