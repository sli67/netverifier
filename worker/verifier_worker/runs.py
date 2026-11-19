"""Runs one run_workflow job: starts a run of the staged workflow, or resumes one.

The workflow's working state lives in LangGraph's checkpoint tables, keyed by
the run id. The `runs` row holds what the dashboard shows and is updated as
each step starts and finishes.
"""

import hashlib
import logging
import tempfile
from pathlib import Path
from typing import Any

import psycopg
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from . import agent, batfish, checks, docs, handlers, jobs, storage, workflow
from .structured import StructuredError, make_ask
from .config import Config
from .jobs import Job

log = logging.getLogger("worker")

ENDED = ("done", "failed", "cancelled")
RUN_FAILED = "The run stopped because of an internal error. Start it again."
RUN_ABANDONED = "The worker stopped responding during this run. Start it again."
# Steps in one run, counting every repair round. Far above what a run needs.
RECURSION_LIMIT = 60
# Agent steps for a run's investigation. Lower than in chat: it only gathers
# what the checks need, and the checks themselves are run by code afterwards.
INVESTIGATE_STEPS = 40


class Cancelled(Exception):
    """The run was cancelled or removed while it was being worked on."""


def setup(database_url: str) -> None:
    """Create LangGraph's checkpoint tables if they are not there yet."""
    with psycopg.connect(database_url, autocommit=True, row_factory=dict_row) as conn:
        PostgresSaver(conn).setup()


def _finish(run_id: str, status: str, values: dict[str, Any] | None = None, error: str | None = None) -> handlers.Apply:
    def apply(c: psycopg.Connection) -> None:
        c.execute(
            """
            update runs
               set status = %(status)s, error = %(error)s, updated_at = now(),
                   outcome = coalesce(%(outcome)s, outcome),
                   state = coalesce(%(state)s, state),
                   finished_at = case when %(status)s in ('done', 'failed') then now() end
             where id = %(id)s and status not in ('done', 'failed', 'cancelled')
            """,
            {
                "id": run_id,
                "status": status,
                "error": error,
                "outcome": (values or {}).get("outcome"),
                "state": Jsonb(workflow.public(values)) if values else None,
            },
        )

    return apply


def failed(conn: psycopg.Connection, run_id: str | None, error: str = RUN_FAILED) -> None:
    if run_id:
        _finish(run_id, "failed", error=error)(conn)


def abandoned(conn: psycopg.Connection, investigation_id: str) -> None:
    conn.execute(
        """
        update runs set status = 'failed', error = %s, updated_at = now(), finished_at = now()
         where investigation_id = %s and status in ('queued', 'running')
        """,
        (RUN_ABANDONED, investigation_id),
    )


def content_hash(entries: list[dict[str, Any]]) -> str:
    """The same hash the API computes for a snapshot: over each file's hash and path, in path order."""
    listing = "".join(f"{e['sha256']}  {e['path']}\n" for e in entries)
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def run(config: Config, conn: psycopg.Connection, s3, job: Job, model: BaseChatModel | None) -> handlers.Apply:
    run_id = job.run_id
    row = conn.execute("select mode, intent, status, target from runs where id = %s", (run_id,)).fetchone()
    conn.commit()
    if row is None or row[2] in ENDED:
        return lambda c: None
    mode, intent, target = row[0], row[1], row[3]
    if model is None:
        return _finish(run_id, "failed", error=handlers.NOT_CONFIGURED)

    snap = handlers._snapshot(conn, job.investigation_id)
    with tempfile.TemporaryDirectory(prefix="verifier-snapshot-") as tmp:
        root = storage.download_snapshot(s3, config.s3_bucket, snap["prefix"], snap["files"], Path(tmp))
        files = {
            entry["path"]: (root / "configs" / entry["path"]).read_text(encoding="utf-8", errors="replace")
            for entry in snap["files"]
        }
        bf, _, version = batfish.ensure_model(config.batfish_host, snap["content_hash"], lambda: root)
    base = batfish.model_name(snap["content_hash"], version)
    base_report = snap["result"] or {"files": [], "issues": []}

    index = docs.get_index(config.database_url, config.openai_api_key, config.embedding_model)
    ctx = agent.Context(
        snapshot_name=snap["name"],
        files=files,
        parsed_files=base_report["files"],
        intent=intent,
        batfish=bf,
        search_docs=index.search if index else None,
        dataplane_error=handlers.dataplane_error(snap["result"], bf),
        overview=handlers.network_overview(bf, files, base_report["files"]),
    )

    # Progress and checkpoints are written as the run goes, outside the job's
    # own transaction, so the dashboard can follow along.
    # The checkpointer gets a connection of its own: it pipelines its writes,
    # which other statements on the same connection must not be mixed into.
    with (
        psycopg.connect(config.database_url, autocommit=True) as side,
        psycopg.connect(config.database_url, autocommit=True, row_factory=dict_row) as checkpoints,
    ):

        def publish(stage: str | None = None, values: dict[str, Any] | None = None) -> None:
            cur = side.execute(
                """
                update runs
                   set status = 'running', stage = coalesce(%s, stage), state = coalesce(%s, state), updated_at = now()
                 where id = %s and status not in ('done', 'failed', 'cancelled')
                """,
                (stage, Jsonb(workflow.public(values)) if values else None, run_id),
            )
            if cur.rowcount != 1:
                raise Cancelled(run_id)

        def investigate(task: str) -> tuple[str, list[dict[str, Any]]]:
            def progress(update: dict[str, Any]) -> None:
                jobs.set_progress(side, job, update)

            try:
                return agent.reply(model, ctx, [HumanMessage(task)], progress, max_steps=INVESTIGATE_STEPS)
            except agent.AgentGaveUp:
                return "The investigation used all its steps without reaching findings.", []

        def make_candidate(number: int, new_files: dict[str, str], summary: str, diff: str) -> dict[str, Any]:
            entries = []
            for path in sorted(new_files):
                data = new_files[path].encode("utf-8")
                entries.append(
                    {
                        "path": path,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                        "changed": new_files[path] != files[path],
                    }
                )
            digest = content_hash(entries)
            prefix = f"runs/{run_id}/candidates/{number}/"
            changed = [e["path"] for e in entries if e["changed"]]
            for path in changed:
                storage.put_text(s3, config.s3_bucket, f"{prefix}configs/{path}", new_files[path])
            stored = side.execute(
                """
                insert into candidates (run_id, round, content_hash, s3_prefix, files, diff, summary)
                values (%s, %s, %s, %s, %s, %s, %s)
                on conflict (run_id, round) do update
                   set content_hash = excluded.content_hash, s3_prefix = excluded.s3_prefix,
                       files = excluded.files, diff = excluded.diff, summary = excluded.summary
                returning id
                """,
                (run_id, number, digest, prefix, Jsonb(entries), diff, summary),
            ).fetchone()
            name = batfish.model_name(digest, version)
            report = batfish.load_candidate(bf, base, name, new_files)
            return {"id": str(stored[0]), "content_hash": digest, "changed": changed, "snapshot": name, "report": report}

        deps = workflow.Deps(
            ask=make_ask(model),
            investigate=investigate,
            files=files,
            parsed_files=base_report["files"],
            inventory=checks.inventory_of(bf, base),
            run_checks=lambda specs, candidate: checks.run_checks(bf, specs, candidate["snapshot"] if candidate else base),
            side_effects=lambda candidate, specs: checks.side_effects(
                bf, base, candidate["snapshot"], base_report, candidate["report"], specs, candidate["changed"]
            ),
            make_candidate=make_candidate,
            progress=publish,
            describe=lambda name: agent.describe_structure(ctx, name),
            code_findings=lambda name: checks.unreachable_filter_lines(bf, base, name),
        )
        graph = workflow.build(deps, PostgresSaver(checkpoints))
        thread = {"configurable": {"thread_id": run_id}, "recursion_limit": RECURSION_LIMIT}

        state = graph.get_state(thread)
        waiting_for = {i.value.get("kind") for task in state.tasks for i in task.interrupts}
        payload = job.payload or {}
        if not state.values:
            graph_input: Any = {"mode": mode, "intent": intent, "target": target}
        elif payload.get("action") == "answer" and "question" in waiting_for:
            graph_input = Command(resume=payload["answer"])
        elif payload.get("action") == "checks" and "checks" in waiting_for:
            graph_input = Command(resume={k: payload.get(k) for k in ("drop", "note", "by")})
        elif payload.get("action") == "decision" and "approval" in waiting_for:
            graph_input = Command(resume={k: payload.get(k) for k in ("decision", "candidate_hash", "by")})
        else:
            # A retry of a job that already got somewhere: carry on from the last checkpoint.
            graph_input = None

        try:
            publish()
            # The state comes straight from the stream: reading the checkpoint here can return the
            # previous step's state, because checkpoints are written in the background.
            for values in graph.stream(graph_input, thread, stream_mode="values"):
                publish(values=values)
            final = graph.get_state(thread)
        except Cancelled:
            log.info("run %s was cancelled while in progress", run_id)
            return lambda c: None
        except (workflow.WorkflowError, StructuredError) as error:
            log.info("run %s cannot continue: %s", run_id, error)
            return _finish(run_id, "failed", graph.get_state(thread).values, str(error))

    if "ask_user" in final.next or "review_checks" in final.next:
        status = "waiting_user"
    elif "approve" in final.next:
        status = "waiting_approval"
    else:
        status = "done"
    log.info("run %s is %s (%s)", run_id, status, final.values.get("outcome") or "no outcome yet")
    return _finish(run_id, status, final.values)
