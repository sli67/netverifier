"""Worker entry point: claim jobs from Postgres and run them, one at a time.

Each worker process is paired with one Batfish instance. To run several
investigations at once, start several worker and Batfish pairs.

`python -m verifier_worker --deploy` runs the deploy worker instead: it takes
only deploy_change jobs, needs lab access, and needs no model.
"""

import logging
import signal
import sys
import threading

import psycopg

from . import config as config_module, deploy, handlers, jobs, runs, storage
from .config import Config

log = logging.getLogger("worker")


def _heartbeat_loop(config: Config, job: jobs.Job, stop: threading.Event, lost: threading.Event) -> None:
    """Keep the lease alive on a separate connection while the job runs."""
    interval = max(1.0, config.lease_seconds / 3)
    try:
        with psycopg.connect(config.database_url) as conn:
            while not stop.wait(interval):
                if not jobs.heartbeat(conn, job, config.lease_seconds):
                    lost.set()
                    return
    except psycopg.Error:
        # Cannot reach the database: the lease will expire and another worker
        # will take over. Completion is token-guarded, so that is safe.
        log.exception("heartbeat failed for job %s", job.id)


def run_job(config: Config, conn: psycopg.Connection, s3, model, job: jobs.Job) -> None:
    stop, lost = threading.Event(), threading.Event()
    beat = threading.Thread(target=_heartbeat_loop, args=(config, job, stop, lost), daemon=True)
    beat.start()
    try:
        try:
            if job.kind == "parse_snapshot":
                apply = handlers.run_parse(config, conn, s3, job)
            elif job.kind == "agent_reply":
                apply = handlers.run_agent(config, conn, s3, job, model)
            elif job.kind == "run_workflow":
                apply = runs.run(config, conn, s3, job, model)
            elif job.kind == "deploy_change":
                apply = deploy.run(config, conn, s3, job)
            else:
                raise RuntimeError(f"unknown job kind {job.kind!r}")
            # Recording the result can fail too (a constraint, a lost connection). That fails
            # this job like any other error, rather than stopping the worker.
            jobs.complete(conn, job, apply)
        except jobs.LeaseLost:
            raise
        except Exception as error:  # noqa: BLE001 - any failure is recorded against the job
            conn.rollback()
            log.exception("job %s attempt %d failed", job.id, job.attempt)
            message = f"{type(error).__name__}: {error}"
            if job.kind == "parse_snapshot":
                retry = jobs.fail(
                    conn,
                    job,
                    message,
                    on_retry=lambda c: handlers.parse_retry(c, job),
                    on_final=lambda c, e: handlers.parse_failed(c, job.investigation_id, e),
                )
            elif job.kind == "deploy_change":
                # A retry rebuilds the lab from the current configs, so it is safe.
                retry = jobs.fail(
                    conn,
                    job,
                    message,
                    on_retry=lambda c: None,
                    on_final=lambda c, e: deploy.failed(c, job.deployment_id, e),
                )
            elif job.kind == "run_workflow":
                # A retry carries on from the run's last checkpoint.
                retry = jobs.fail(
                    conn,
                    job,
                    message,
                    on_retry=lambda c: None,
                    on_final=lambda c, e: runs.failed(c, job.run_id),
                )
            else:
                retry = jobs.fail(
                    conn,
                    job,
                    message,
                    on_retry=lambda c: None,
                    on_final=lambda c, e: handlers.agent_failed(c, job.investigation_id),
                )
            log.info("job %s %s", job.id, "will be retried" if retry else "failed permanently")
            return
        log.info("job %s done", job.id)
    except jobs.LeaseLost:
        conn.rollback()
        log.warning("job %s: lease was lost, discarding this attempt's result", job.id)
    finally:
        stop.set()
        beat.join()


def make_model(config: Config):
    """The chat model for the agent, or None when no key is configured."""
    if not config.deepseek_api_key:
        return None
    from langchain_deepseek import ChatDeepSeek

    return ChatDeepSeek(model=config.agent_model, api_key=config.deepseek_api_key, max_retries=2, timeout=180)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = config_module.load()
    s3 = storage.make_client(config)
    deploying = "--deploy" in sys.argv[1:]
    kinds = jobs.DEPLOY_KINDS if deploying else jobs.AGENT_KINDS
    model = None if deploying else make_model(config)

    shutdown = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: shutdown.set())

    if deploying:
        log.info(
            "deploy worker %s started (batfish instance %s, lab %s, image %s)",
            config.worker_id,
            config.batfish_instance,
            config.lab_kind,
            config.lab_image or {"frr": "quay.io/frrouting/frr:10.2.1 (default)"}.get(config.lab_kind, "not configured"),
        )
    else:
        log.info(
            "worker %s started (batfish instance %s, agent model %s, documentation search %s)",
            config.worker_id,
            config.batfish_instance,
            config.agent_model if model else "not configured",
            "on" if config.openai_api_key else "off",
        )
    # Request logging from the HTTP client is noise at INFO level.
    for noisy in ("httpx", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    runs.setup(config.database_url)
    with psycopg.connect(config.database_url) as conn:
        while not shutdown.is_set():
            abandoned = jobs.fail_abandoned(conn, handlers.record_abandoned)
            if abandoned:
                log.warning("failed %d abandoned job(s)", abandoned)
            job = jobs.claim(conn, config.worker_id, config.lease_seconds, kinds)
            if job is None:
                shutdown.wait(config.poll_seconds)
                continue
            log.info("claimed job %s (%s), attempt %d of %d", job.id, job.kind, job.attempt, job.max_attempts)
            run_job(config, conn, s3, model, job)
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
