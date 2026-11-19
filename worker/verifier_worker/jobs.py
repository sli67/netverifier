"""Job queue on Postgres: claim with a lease, heartbeat, and token-guarded completion.

A worker holds a job only while its lease is unexpired. Each claim issues a new
attempt_token, and every later write is conditional on that token. A worker that
stalled past its lease therefore cannot overwrite the work of the worker that
took the job over.
"""

from dataclasses import dataclass
from typing import Any, Callable

import psycopg
from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    investigation_id: str
    attempt: int
    max_attempts: int
    token: str
    # run_workflow jobs only: the run, and whether to start it or resume it with an answer or decision.
    run_id: str | None = None
    payload: dict[str, Any] | None = None
    # deploy_change jobs only.
    deployment_id: str | None = None


# Which worker takes which jobs. The deploy worker needs lab access the agent
# worker must not have, so each claims only its own kinds.
AGENT_KINDS = ("parse_snapshot", "agent_reply", "run_workflow")
DEPLOY_KINDS = ("deploy_change",)


class LeaseLost(Exception):
    """This worker no longer holds the job; its results must be discarded."""


def claim(
    conn: psycopg.Connection, worker_id: str, lease_seconds: int, kinds: tuple[str, ...] = AGENT_KINDS
) -> Job | None:
    """Take the oldest runnable job of these kinds, including one whose previous holder's lease expired."""
    with conn.transaction():
        row = conn.execute(
            """
            update jobs
               set status = 'running',
                   attempts = attempts + 1,
                   attempt_token = gen_random_uuid(),
                   locked_by = %(worker)s,
                   lease_expires_at = now() + make_interval(secs => %(lease)s),
                   updated_at = now()
             where id = (
                     select id from jobs
                      where attempts < max_attempts
                        and kind = any(%(kinds)s)
                        and ((status = 'queued' and run_after <= now())
                          or (status = 'running' and lease_expires_at < now()))
                      order by created_at
                      for update skip locked
                      limit 1)
            returning id, kind, investigation_id, attempts, max_attempts, attempt_token, run_id, payload, deployment_id
            """,
            {"worker": worker_id, "lease": lease_seconds, "kinds": list(kinds)},
        ).fetchone()
        if row is None:
            return None
        run_id = str(row[6]) if row[6] else None
        deployment_id = str(row[8]) if row[8] else None
        job = Job(str(row[0]), row[1], str(row[2]), row[3], row[4], str(row[5]), run_id, row[7], deployment_id)
        conn.execute(
            """
            update investigations
               set status = 'parsing', started_at = coalesce(started_at, now())
             where id = %s and status in ('queued', 'parsing') and %s = 'parse_snapshot'
            """,
            (job.investigation_id, job.kind),
        )
        return job


def heartbeat(conn: psycopg.Connection, job: Job, lease_seconds: int) -> bool:
    """Extend the lease. Returns False when the job is no longer ours."""
    with conn.transaction():
        cur = conn.execute(
            """
            update jobs set lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
             where id = %s and attempt_token = %s and status = 'running'
            """,
            (lease_seconds, job.id, job.token),
        )
        return cur.rowcount == 1


def set_progress(conn: psycopg.Connection, job: Job, progress: dict[str, Any]) -> None:
    """Record what the job is doing now. conn must be autocommit and not the job's own connection."""
    conn.execute(
        "update jobs set progress = %s where id = %s and attempt_token = %s and status = 'running'",
        (Jsonb(progress), job.id, job.token),
    )


def _finish_job(conn: psycopg.Connection, job: Job, status: str, error: str | None) -> None:
    cur = conn.execute(
        """
        update jobs
           set status = %s, last_error = %s, lease_expires_at = null, updated_at = now()
         where id = %s and attempt_token = %s and status = 'running'
        """,
        (status, error, job.id, job.token),
    )
    if cur.rowcount != 1:
        raise LeaseLost(job.id)


def complete(conn: psycopg.Connection, job: Job, apply: Callable[[psycopg.Connection], None]) -> None:
    """Mark the job done and let `apply` record its result, in one transaction.

    Raises LeaseLost, recording nothing, when this worker no longer holds the job.
    """
    with conn.transaction():
        _finish_job(conn, job, "done", None)
        apply(conn)


def fail(
    conn: psycopg.Connection,
    job: Job,
    error: str,
    on_retry: Callable[[psycopg.Connection], None],
    on_final: Callable[[psycopg.Connection, str], None],
) -> bool:
    """Record a failed attempt. Returns True when the job will be retried."""
    retry = job.attempt < job.max_attempts
    with conn.transaction():
        if retry:
            cur = conn.execute(
                """
                update jobs
                   set status = 'queued', last_error = %s, lease_expires_at = null,
                       run_after = now() + make_interval(secs => %s), updated_at = now()
                 where id = %s and attempt_token = %s and status = 'running'
                """,
                (error, 5 * job.attempt, job.id, job.token),
            )
            if cur.rowcount != 1:
                raise LeaseLost(job.id)
            on_retry(conn)
        else:
            _finish_job(conn, job, "failed", error)
            on_final(conn, error)
    return retry


def fail_abandoned(
    conn: psycopg.Connection,
    on_final: Callable[[psycopg.Connection, str, str], None],
) -> int:
    """Fail jobs whose last allowed attempt died without reporting back.

    on_final(conn, kind, investigation_id) records the failure where users see it.
    """
    with conn.transaction():
        rows = conn.execute(
            """
            update jobs
               set status = 'failed', lease_expires_at = null, updated_at = now(),
                   last_error = coalesce(last_error, 'worker stopped responding')
             where status = 'running' and lease_expires_at < now() and attempts >= max_attempts
            returning kind, investigation_id
            """
        ).fetchall()
        for kind, investigation_id in rows:
            on_final(conn, kind, str(investigation_id))
        return len(rows)
