"""Retry schedule of the outbox worker. Time is injected, so nothing here sleeps."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import worker
from app.models import AuditEvent, OutboxCommand, SystemIssue, utcnow
from app.worker import MAX_ATTEMPTS, backoff_seconds, process_pending
from tests.conftest import post_intake

MARKER = "PII-MARKER-must-not-be-stored"


def test_backoff_doubles_and_is_capped():
    assert [backoff_seconds(n) for n in range(1, 8)] == [30, 60, 120, 240, 480, 900, 900]


@pytest.fixture
def case_with_intake_done(client, app, payload):
    """A case whose two intake commands are already processed, so only test commands remain."""
    case_id = post_intake(client, payload).json()["case_id"]
    t0 = utcnow()
    with app.state.sessionmaker() as s:
        assert process_pending(s, now=t0) == 2
    return case_id, t0


def add_command(app, name, case_id, due):
    with app.state.sessionmaker() as s:
        cmd = OutboxCommand(command=name, case_id=case_id, next_attempt_at=due)
        s.add(cmd)
        s.commit()
        return cmd.id


def events(session, event_type, case_id):
    return session.scalars(select(AuditEvent).where(
        AuditEvent.event_type == event_type, AuditEvent.case_id == case_id)).all()


def test_transient_failures_are_retried_with_growing_pauses_until_success(
    app, case_with_intake_done, monkeypatch
):
    case_id, t0 = case_with_intake_done
    calls = []

    def flaky(session, case):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(f"temporary: {MARKER}")
        return {"flaky": "ok"}

    monkeypatch.setitem(worker.HANDLERS, "FLAKY", flaky)
    cmd_id = add_command(app, "FLAKY", case_id, due=t0)

    with app.state.sessionmaker() as s:
        assert process_pending(s, now=t0) == 1           # attempt 1 fails
        cmd = s.get(OutboxCommand, cmd_id)
        assert (cmd.status, cmd.attempts) == ("PENDING", 1)
        assert cmd.next_attempt_at == t0 + timedelta(seconds=30)
        assert s.scalars(select(SystemIssue)).all() == []   # not a failure yet

        assert process_pending(s, now=t0 + timedelta(seconds=10)) == 0   # not due: left alone
        assert len(calls) == 1

        assert process_pending(s, now=t0 + timedelta(seconds=30)) == 1   # attempt 2 fails
        assert s.get(OutboxCommand, cmd_id).next_attempt_at == t0 + timedelta(seconds=90)  # +60s

        assert process_pending(s, now=t0 + timedelta(seconds=90)) == 1   # attempt 3 succeeds
        cmd = s.get(OutboxCommand, cmd_id)
        assert (cmd.status, cmd.attempts) == ("DONE", 3)
        assert s.scalars(select(SystemIssue)).all() == []
        assert len(events(s, "WORKER_COMMAND_RETRY", case_id)) == 2
        assert len(events(s, "WORKER_COMMAND_FAILED", case_id)) == 0


def test_a_command_that_never_succeeds_fails_visibly_after_max_attempts(
    app, case_with_intake_done, monkeypatch
):
    case_id, t0 = case_with_intake_done

    def always_down(session, case):
        raise RuntimeError(f"database down: {MARKER}")

    monkeypatch.setitem(worker.HANDLERS, "DOWN", always_down)
    cmd_id = add_command(app, "DOWN", case_id, due=t0)

    now = t0
    with app.state.sessionmaker() as s:
        for _ in range(MAX_ATTEMPTS):
            assert process_pending(s, now=now) == 1
            cmd = s.get(OutboxCommand, cmd_id)
            now = cmd.next_attempt_at
        assert (cmd.status, cmd.attempts) == ("FAILED", MAX_ATTEMPTS)
        assert process_pending(s, now=now + timedelta(days=1)) == 0   # no further attempts

        issues = s.scalars(select(SystemIssue)).all()
        assert [i.kind for i in issues] == ["WORKER_COMMAND_FAILED"]     # exactly one, at the end
        assert f"after {MAX_ATTEMPTS} attempt" in issues[0].detail
        assert len(events(s, "WORKER_COMMAND_RETRY", case_id)) == MAX_ATTEMPTS - 1
        failed = events(s, "WORKER_COMMAND_FAILED", case_id)
        assert len(failed) == 1 and f'"attempts": {MAX_ATTEMPTS}' in failed[0].detail


def test_error_text_of_transient_failures_never_contains_submitted_values(
    app, case_with_intake_done, monkeypatch
):
    case_id, t0 = case_with_intake_done

    def boom(session, case):
        raise RuntimeError(f"INSERT ... VALUES ('{MARKER}')")

    monkeypatch.setitem(worker.HANDLERS, "BOOM", boom)
    cmd_id = add_command(app, "BOOM", case_id, due=t0)
    with app.state.sessionmaker() as s:
        process_pending(s, now=t0)
        cmd = s.get(OutboxCommand, cmd_id)
        assert cmd.error == "RuntimeError"
        retry = events(s, "WORKER_COMMAND_RETRY", case_id)[0]
        assert MARKER not in retry.detail and "RuntimeError" in retry.detail


@pytest.mark.parametrize("name,case", [("NO_SUCH_COMMAND", "case"), ("CREATE_REVIEW_ITEM", "none")])
def test_permanent_errors_fail_immediately_without_retries(
    app, case_with_intake_done, name, case
):
    case_id, t0 = case_with_intake_done
    cmd_id = add_command(app, name, case_id if case == "case" else "no-such-case", due=t0)
    with app.state.sessionmaker() as s:
        assert process_pending(s, now=t0) == 1
        cmd = s.get(OutboxCommand, cmd_id)
        assert (cmd.status, cmd.attempts) == ("FAILED", 1)
        assert s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "WORKER_COMMAND_RETRY")).all() == []
        assert len(s.scalars(select(SystemIssue)).all()) == 1


def test_retry_events_show_up_in_the_case_timeline(client, app, case_with_intake_done, monkeypatch):
    from tests.conftest import as_identity

    case_id, t0 = case_with_intake_done
    monkeypatch.setitem(worker.HANDLERS, "FLAKY", lambda s, c: (_ for _ in ()).throw(OSError()))
    add_command(app, "FLAKY", case_id, due=t0)
    with app.state.sessionmaker() as s:
        process_pending(s, now=t0)
    timeline = client.get(f"/api/cases/{case_id}/audit", headers=as_identity("disp-1")).json()
    assert "WORKER_COMMAND_RETRY" in [e["event_type"] for e in timeline]
