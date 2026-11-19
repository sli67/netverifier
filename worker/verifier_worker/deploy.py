"""Runs one deploy_change job: puts an approved candidate in the lab and verifies it there.

    baseline   boot the lab on the snapshot's current configs; read the live state
    candidate  boot it on the approved candidate; read the live state again
    verify     compare the live lab with Batfish's prediction for the candidate
    rollback   on a mismatch, boot the current configs again

Every boot rebuilds the whole lab from its configs, so retrying a deployment
that died half way is safe: it starts from the current configs again rather
than guessing which devices were already changed.
"""

import hashlib
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb

from . import batfish, handlers, lab, network, runs, storage
from .config import Config
from .jobs import Job

log = logging.getLogger("worker")

ENDED = ("completed", "rolled_back", "failed")
ABANDONED = "The deploy worker stopped responding during this deployment. Start it again."


def _load(conn: psycopg.Connection, deployment_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select d.status, d.candidate_hash, d.snapshot_id, r.id, c.content_hash, c.s3_prefix, c.files,
               s.content_hash, s.s3_prefix, s.files, i.result,
               (select a.decision from approvals a where a.run_id = r.id and a.candidate_hash = d.candidate_hash)
          from deployments d
          join runs r on r.id = d.run_id
          join candidates c on c.id = d.candidate_id
          join snapshots s on s.id = d.snapshot_id
          join investigations i on i.id = r.investigation_id
         where d.id = %s
        """,
        (deployment_id,),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    keys = ("status", "approved_hash", "snapshot_id", "run_id", "candidate_hash", "candidate_prefix", "candidate_files",
            "snapshot_hash", "snapshot_prefix", "snapshot_files", "result", "decision")  # fmt: skip
    return dict(zip(keys, row))


def _texts(s3, bucket: str, d: dict[str, Any], root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(current, candidate) config text per file path, each verified against its recorded hashes."""
    current = {e["path"]: (root / "configs" / e["path"]).read_text(encoding="utf-8") for e in d["snapshot_files"]}
    candidate = dict(current)
    for entry in d["candidate_files"]:
        if entry["changed"]:
            key = f"{d['candidate_prefix']}configs/{entry['path']}"
            with s3.get_object(Bucket=bucket, Key=key)["Body"] as body:
                candidate[entry["path"]] = body.read().decode("utf-8")
        if hashlib.sha256(candidate[entry["path"]].encode("utf-8")).hexdigest() != entry["sha256"]:
            raise lab.LabError(f"the stored candidate file {entry['path']} does not match its recorded hash")
    if runs.content_hash(d["candidate_files"]) != d["approved_hash"]:
        raise lab.LabError("the candidate's files do not add up to the approved content hash")
    return current, candidate


def _by_node(texts: dict[str, str], parsed: list[dict[str, Any]]) -> dict[str, str]:
    out = {}
    for entry in parsed:
        nodes = entry.get("nodes") or []
        if len(nodes) == 1 and entry["path"] in texts:
            out[nodes[0]] = texts[entry["path"]]
    return out


def run(config: Config, conn: psycopg.Connection, s3, job: Job, tools: dict[str, Callable] | None = None) -> handlers.Apply:
    """tools replaces the lab's deploy, addresses and read functions in tests."""
    tools = tools or {}
    d = _load(conn, job.deployment_id)
    if d is None or d["status"] in ENDED:
        return lambda c: None
    parsed = (d["result"] or {}).get("files", [])
    name = lab.lab_name(str(d["snapshot_id"]))

    with psycopg.connect(config.database_url, autocommit=True) as side:

        def publish(status: str, stage: str, message: str, result: dict | None = None) -> None:
            entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "step": stage, "message": message}
            side.execute(
                """
                update deployments
                   set status = %s, stage = %s, lab_name = %s, log = log || %s::jsonb,
                       result = coalesce(%s, result), updated_at = now()
                 where id = %s and status not in ('completed', 'rolled_back', 'failed')
                """,
                (status, stage, name, Jsonb([entry]), Jsonb(result) if result else None, job.deployment_id),
            )
            log.info("deployment %s: %s", job.deployment_id, message)

        try:
            if d["decision"] != "approved":
                raise lab.LabError("this candidate is not approved, so it cannot be deployed")
            kind = config.lab_kind
            formats = {f["format"] for f in parsed if f.get("nodes")}
            runnable = lab.FORMATS.get(kind, ())
            if not formats or not formats <= set(runnable):
                raise lab.LabError(
                    f"the {kind} lab runs {', '.join(runnable) or 'no'} configs; this snapshot has {', '.join(sorted(formats))}"
                )
            image = config.lab_image or lab.DEFAULT_IMAGES.get(kind)
            if not image:
                raise lab.LabError("no lab image is configured: set LAB_IMAGE in worker/.env, e.g. vrnetlab/cisco_iol:17.12.01")

            with tempfile.TemporaryDirectory(prefix="verifier-deploy-") as tmp:
                root = storage.download_snapshot(s3, config.s3_bucket, d["snapshot_prefix"], d["snapshot_files"], Path(tmp))
                current, candidate = _texts(s3, config.s3_bucket, d, root)
                bf, _, version = batfish.ensure_model(config.batfish_host, d["snapshot_hash"], lambda: root)
            base = batfish.model_name(d["snapshot_hash"], version)
            changed = batfish.model_name(d["approved_hash"], version)
            batfish.load_candidate(bf, base, changed, candidate)  # reuses the model if Batfish still has it
            links = network.overview(bf)["links"]
            current_nodes, candidate_nodes = _by_node(current, parsed), _by_node(candidate, parsed)
            directory = Path(config.lab_dir) / name
            deploy_lab = tools.get("deploy") or (lambda topo: lab.deploy(config.containerlab, topo))
            find = tools.get("addresses") or (lambda nodes: lab.addresses(name, nodes, kind=kind))
            read = tools.get("read") or (lab.read_frr if kind == "frr" else lab.read_device)

            def boot(texts: dict[str, str], tag: str, model: str) -> tuple[dict, dict, dict]:
                topo, renamed = lab.write_lab(directory, name, kind, image, texts, links, tag, config.lab_mgmt_subnet)
                deploy_lab(topo)
                hosts = find(sorted(texts))
                predicted = lab.predict(bf, model)
                live = lab.converge(hosts, predicted, config.lab_username, config.lab_password,
                                    config.lab_converge_seconds, read=read, **tools.get("timing", {}))  # fmt: skip
                return renamed, predicted, live

            publish("deploying", "baseline", "Booting the lab on the current configs.")
            renamed, predicted, live = boot(current_nodes, "baseline", base)
            before = {"predicted": predicted, "live": live, "comparison": lab.compare(predicted, live)}
            result: dict[str, Any] = {"lab": name, "interfaces": renamed, "before": before}
            agrees = "matches" if before["comparison"]["ok"] else "does not match"
            publish("deploying", "baseline", f"With the current configs, the live lab {agrees} Batfish.", result)

            publish("deploying", "candidate", "Booting the lab on the approved candidate.", result)
            _, predicted, live = boot(candidate_nodes, d["approved_hash"][:8], changed)
            publish("verifying", "verify", "Comparing the live lab with Batfish's prediction for the candidate.", result)
            result["after"] = {"predicted": predicted, "live": live, "comparison": lab.compare(predicted, live)}

            if result["after"]["comparison"]["ok"]:
                status, message = "completed", "The live lab matches Batfish's prediction for the candidate."
            else:
                publish("verifying", "rollback", "The live lab differs from the prediction; booting the current configs again.", result)
                boot(current_nodes, "baseline", base)
                status, message = "rolled_back", "The live lab did not match the prediction, so the current configs were restored."
        except lab.LabError as error:
            status, message, result = "failed", str(error), None

    stage = {"completed": "verify", "rolled_back": "rollback"}.get(status, None)

    def apply(c: psycopg.Connection) -> None:
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "step": stage or "failed", "message": message}
        c.execute(
            """
            update deployments
               set status = %s, stage = coalesce(%s, stage), error = %s, result = coalesce(%s, result),
                   log = log || %s::jsonb, updated_at = now(), finished_at = now()
             where id = %s and status not in ('completed', 'rolled_back', 'failed')
            """,
            (status, stage, message if status == "failed" else None, Jsonb(result) if result else None,
             Jsonb([entry]), job.deployment_id),  # fmt: skip
        )

    log.info("deployment %s is %s", job.deployment_id, status)
    return apply


def failed(conn: psycopg.Connection, deployment_id: str | None, error: str) -> None:
    if deployment_id:
        conn.execute(
            """
            update deployments set status = 'failed', error = %s, updated_at = now(), finished_at = now()
             where id = %s and status not in ('completed', 'rolled_back', 'failed')
            """,
            (error[:500], deployment_id),
        )


def abandoned(conn: psycopg.Connection, investigation_id: str) -> None:
    conn.execute(
        """
        update deployments set status = 'failed', error = %s, updated_at = now(), finished_at = now()
         where status in ('queued', 'deploying', 'verifying')
           and run_id in (select id from runs where investigation_id = %s)
        """,
        (ABANDONED, investigation_id),
    )
