"""Job queue behaviour against the private test database.

Run with `pnpm test:worker` from the repository root, which creates and
migrates that database first.
"""

import os
import uuid

import psycopg
import pytest

from verifier_worker import handlers, jobs

DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgres://verifier:verifier@127.0.0.1:5432/verifier_test")


def set_status(investigation, status, error=None):
    """A stand-in for a handler's result: just record a status on the investigation."""

    def apply(c):
        c.execute("update investigations set status = %s, error = %s where id = %s", (status, error, investigation))

    return apply


def fail(conn, job, error):
    return jobs.fail(
        conn,
        job,
        error,
        on_retry=lambda c: handlers.parse_retry(c, job),
        on_final=lambda c, e: handlers.parse_failed(c, job.investigation_id, e),
    )


@pytest.fixture
def conn():
    with psycopg.connect(DATABASE_URL) as connection:
        yield connection


@pytest.fixture
def investigation(conn):
    """A queued investigation with one job, removed afterwards.

    The queue is emptied first so each test claims its own job.
    """
    user, snapshot = uuid.uuid4(), uuid.uuid4()
    conn.execute("delete from jobs")
    conn.execute(
        "insert into users (id, email, password_hash, role) values (%s, %s, 'x', 'requester')",
        (user, f"worker-test-{user}@example.test"),
    )
    conn.execute(
        """insert into snapshots (id, name, s3_prefix, content_hash, files, file_count, total_bytes, created_by)
           values (%s, 'worker test', %s, 'hash', '[]', 0, 0, %s)""",
        (snapshot, f"snapshots/{snapshot}/", user),
    )
    inv = conn.execute(
        "insert into investigations (snapshot_id, created_by) values (%s, %s) returning id", (snapshot, user)
    ).fetchone()[0]
    conn.execute("insert into jobs (kind, investigation_id) values ('parse_snapshot', %s)", (inv,))
    conn.commit()
    yield str(inv)
    conn.rollback()
    conn.execute("delete from jobs where investigation_id = %s", (inv,))
    conn.execute("delete from messages where investigation_id = %s", (inv,))
    conn.execute("delete from investigations where id = %s", (inv,))
    conn.execute("delete from snapshots where id = %s", (snapshot,))
    conn.execute("delete from users where id = %s", (user,))
    conn.commit()


def state(conn, investigation):
    row = conn.execute(
        """select j.status, j.attempts, i.status, i.error from jobs j
           join investigations i on i.id = j.investigation_id where i.id = %s""",
        (investigation,),
    ).fetchone()
    conn.commit()
    return row


def expire_lease(conn, investigation):
    conn.execute(
        "update jobs set lease_expires_at = now() - interval '1 second' where investigation_id = %s",
        (investigation,),
    )
    conn.commit()


def test_a_job_is_claimed_by_only_one_worker(conn, investigation):
    first = jobs.claim(conn, "worker-a", 60)
    with psycopg.connect(DATABASE_URL) as other:
        second = jobs.claim(other, "worker-b", 60)
    assert first is not None and first.investigation_id == investigation
    assert second is None
    assert state(conn, investigation)[:3] == ("running", 1, "parsing")


def test_completing_records_the_result(conn, investigation):
    job = jobs.claim(conn, "worker-a", 60)
    jobs.complete(conn, job, set_status(investigation, 'ready'))
    assert state(conn, investigation) == ("done", 1, "ready", None)
    assert jobs.claim(conn, "worker-a", 60) is None


def test_expired_lease_is_taken_over_and_the_stale_worker_cannot_commit(conn, investigation):
    stale = jobs.claim(conn, "worker-a", 60)
    expire_lease(conn, investigation)

    takeover = jobs.claim(conn, "worker-b", 60)
    assert takeover is not None and takeover.token != stale.token and takeover.attempt == 2

    assert jobs.heartbeat(conn, stale, 60) is False
    with pytest.raises(jobs.LeaseLost):
        jobs.complete(conn, stale, set_status(investigation, "failed", "stale result"))
    assert state(conn, investigation)[:3] == ("running", 2, "parsing")

    jobs.complete(conn, takeover, set_status(investigation, 'ready'))
    assert state(conn, investigation) == ("done", 2, "ready", None)


def test_failures_are_retried_then_become_permanent(conn, investigation):
    for attempt in (1, 2):
        job = jobs.claim(conn, "worker-a", 60)
        assert job.attempt == attempt
        assert fail(conn, job, "boom") is True
        assert state(conn, investigation)[:3] == ("queued", attempt, "queued")
        assert jobs.claim(conn, "worker-a", 60) is None  # backing off
        conn.execute("update jobs set run_after = now() where investigation_id = %s", (investigation,))
        conn.commit()

    job = jobs.claim(conn, "worker-a", 60)
    assert fail(conn, job, "boom") is False
    assert state(conn, investigation) == ("failed", 3, "failed", "boom")


def test_a_job_abandoned_on_its_last_attempt_is_failed(conn, investigation):
    conn.execute("update jobs set attempts = 2 where investigation_id = %s", (investigation,))
    conn.commit()
    jobs.claim(conn, "worker-a", 60)
    expire_lease(conn, investigation)

    assert jobs.claim(conn, "worker-b", 60) is None
    assert jobs.fail_abandoned(conn, handlers.record_abandoned) >= 1
    status = state(conn, investigation)
    assert status[:3] == ("failed", 3, "failed")
    assert "stopped responding" in status[3]


def test_agent_jobs_leave_the_investigation_status_alone_and_report_failure_as_a_message(conn, investigation):
    conn.execute("delete from jobs where investigation_id = %s", (investigation,))
    conn.execute("update investigations set status = 'ready' where id = %s", (investigation,))
    conn.execute(
        "insert into jobs (kind, investigation_id, max_attempts) values ('agent_reply', %s, 1)", (investigation,)
    )
    conn.commit()

    job = jobs.claim(conn, "worker-a", 60)
    assert job.kind == "agent_reply"
    assert state(conn, investigation)[2] == "ready"  # not moved to 'parsing'

    expire_lease(conn, investigation)
    assert jobs.fail_abandoned(conn, handlers.record_abandoned) >= 1
    assert state(conn, investigation)[:3] == ("failed", 1, "ready")
    messages = conn.execute(
        "select role, failed, content from messages where investigation_id = %s", (investigation,)
    ).fetchall()
    assert messages == [("agent", True, handlers.AGENT_FAILED)]


def test_each_worker_claims_only_its_own_kinds(conn, investigation):
    # A deploy job without a deployment row is enough to test claiming.
    conn.execute("update jobs set kind = 'deploy_change' where investigation_id = %s", (investigation,))
    conn.commit()
    assert jobs.claim(conn, "agent-worker", 60) is None  # agent kinds by default
    job = jobs.claim(conn, "deploy-worker", 60, jobs.DEPLOY_KINDS)
    assert job is not None and job.kind == "deploy_change" and job.deployment_id is None


def test_a_result_that_cannot_be_saved_fails_the_job_not_the_worker(conn, investigation, monkeypatch):
    from types import SimpleNamespace

    from verifier_worker import __main__ as worker_main

    def unsavable(c):
        c.execute("insert into messages (investigation_id, role, content, route) values (%s, 'agent', 'x', 'nonsense')", (investigation,))

    monkeypatch.setattr(handlers, "run_parse", lambda *args: unsavable)
    job = jobs.claim(conn, "worker-a", 60)
    config = SimpleNamespace(lease_seconds=60, database_url=DATABASE_URL)
    worker_main.run_job(config, conn, None, None, job)  # must return, not raise
    status, attempts, *_ = state(conn, investigation)
    # The failed attempt is recorded and the job goes back to the queue for a retry.
    assert (status, attempts) == ("queued", 1)
