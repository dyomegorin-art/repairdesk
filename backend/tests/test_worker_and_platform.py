import json
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, inspect, select

from app.config import Settings
from app.main import create_app
from app.models import (
    AuditEvent,
    IntakeEvent,
    OutboxCommand,
    RawInboundDelivery,
    RepairCase,
    WorkItem,
)
from app.worker import process_pending
from tests.conftest import (
    FIXTURES,
    SEEDED_CASES,
    as_identity,
    build_app,
    count,
    make_settings,
    post_intake,
)


def count_rows(session, model):
    return session.scalar(select(func.count()).select_from(model))


def test_worker_completes_safe_command_and_audits(client, app, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    with app.state.sessionmaker() as s:
        assert process_pending(s) == 2  # VERIFY_INTAKE_RECORD + EVALUATE_ELIGIBILITY
        assert {c.status for c in s.scalars(select(OutboxCommand))} == {"DONE"}
        done = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "WORKER_COMMAND_COMPLETED",
            AuditEvent.detail.contains("VERIFY_INTAKE_RECORD"))).one()
        assert done.case_id == case_id
        assert '"missing_fields": []' in done.detail


def _eligibility_after_worker(client, app, fixture_name):
    payload = json.loads((FIXTURES / fixture_name).read_text())
    case_id = post_intake(client, payload).json()["case_id"]
    with app.state.sessionmaker() as s:
        process_pending(s)
        case = s.get(RepairCase, case_id)
        audit_ev = s.scalars(select(AuditEvent).where(
            AuditEvent.case_id == case_id,
            AuditEvent.detail.contains("EVALUATE_ELIGIBILITY"))).one()
        return case.eligibility_result, audit_ev.detail


@pytest.mark.parametrize("fixture_name,expected,code", [
    ("request_valid.json", "ELIGIBLE", None),
    ("request_owner_review.json", "OWNER_REVIEW", "COMMERCIAL_LOCATION"),
    ("request_emergency.json", "EMERGENCY_STOP", "HAZARD_GAS_SMELL"),
])
def test_worker_stores_eligibility_and_audits_reasons(
    client, app, fixture_name, expected, code
):
    result, detail = _eligibility_after_worker(client, app, fixture_name)
    assert result == expected
    if code:
        assert code in detail


def test_eligibility_result_is_visible_to_the_dispatcher_and_triggers_nothing_else(client, app):
    payload = json.loads((FIXTURES / "request_emergency.json").read_text())
    case_id = post_intake(client, payload).json()["case_id"]
    with app.state.sessionmaker() as s:
        process_pending(s)
        assert count_rows(s, OutboxCommand) == 2  # no follow-up commands: advisory only
    detail = client.get(f"/api/cases/{case_id}", headers=as_identity("disp-1")).json()
    assert detail["eligibility_result"] == "EMERGENCY_STOP"
    assert detail["case_phase"] == "INTAKE"  # the case is not declined or closed automatically


def test_worker_failure_creates_visible_issue_and_audit(client, app):
    with app.state.sessionmaker() as s:
        s.add(OutboxCommand(command="CREATE_REVIEW_ITEM", case_id="no-such-case"))
        s.commit()
        process_pending(s)
        assert s.scalars(select(OutboxCommand)).one().status == "FAILED"
        assert s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "WORKER_COMMAND_FAILED")).one()
    issues = client.get("/api/issues", headers=as_identity("owner-1"))
    assert issues.status_code == 200
    assert issues.json()[0]["kind"] == "WORKER_COMMAND_FAILED"
    assert client.get("/api/issues", headers=as_identity("disp-1")).status_code == 403


def test_committed_state_survives_backend_restart(db_url, payload):
    settings = make_settings(db_url)
    first = build_app(settings)
    case_id = post_intake(TestClient(first), payload).json()["case_id"]
    first.state.engine.dispose()

    second = build_app(settings, create_schema=False)
    r = TestClient(second).get(f"/api/cases/{case_id}", headers=as_identity("disp-1"))
    assert r.status_code == 200
    assert r.json()["case_id"] == case_id


@pytest.mark.parametrize("env", ["staging", "production"])
def test_synthetic_auth_enabled_outside_local_fails_startup(db_url, env):
    with pytest.raises(RuntimeError):
        build_app(make_settings(db_url, app_env=env, allow_local_synthetic_auth=True),
                  create_schema=False)


def test_unknown_env_fails_startup(db_url):
    with pytest.raises(RuntimeError):
        build_app(make_settings(db_url, app_env="prod"), create_schema=False)


def test_synthetic_auth_off_by_default_grants_no_access(db_url, payload):
    app = build_app(make_settings(db_url, allow_local_synthetic_auth=False))
    r = TestClient(app).get("/api/queues/new-requests", headers=as_identity("owner-1"))
    assert r.status_code == 401


def test_client_supplied_role_is_never_trusted(client):
    r = client.get("/api/queues/new-requests",
                   headers={"X-Role": "owner", "X-Synthetic-Identity": "tech-1"})
    assert r.status_code == 403
    r = client.get("/api/queues/new-requests", headers={"X-Role": "owner"})
    assert r.status_code == 401
    r = client.get("/api/queues/new-requests", headers=as_identity("unknown-identity"))
    assert r.status_code == 401


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_alembic_migration_matches_models(db_url):
    """The migrated schema must equal the models: types, nullability, indexes, constraints."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    backend = Path(__file__).resolve().parents[1]
    env = {"DATABASE_URL": db_url, "PATH": "/usr/bin:/usr/local/bin"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=backend, env=env, check=True, capture_output=True)
    from app.db import Base
    engine = create_engine(db_url)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff
    # the circular foreign keys exist
    fks = {fk["name"] for fk in inspect(engine).get_foreign_keys("raw_inbound_delivery")}
    assert {"fk_delivery_event", "fk_delivery_duplicate_of"} <= fks
    engine.dispose()


def test_migrations_can_be_downgraded_to_empty_and_upgraded_again(db_url):
    backend = Path(__file__).resolve().parents[1]
    env = {"DATABASE_URL": db_url, "PATH": "/usr/bin:/usr/local/bin"}
    for args in (["upgrade", "head"], ["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args],
                       cwd=backend, env=env, check=True, capture_output=True)
    engine = create_engine(db_url)
    assert "repair_case" in inspect(engine).get_table_names()
    engine.dispose()


def test_indexes_exist_on_the_filtered_columns(db_url):
    from app.db import Base

    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    indexed = {(t, tuple(ix["column_names"]))
               for t in ("repair_case", "audit_event", "outbox_command")
               for ix in insp.get_indexes(t)}
    assert ("repair_case", ("case_phase",)) in indexed
    assert ("repair_case", ("assigned_technician_id",)) in indexed
    assert ("audit_event", ("case_id",)) in indexed
    assert ("outbox_command", ("status",)) in indexed
    engine.dispose()


def test_missing_database_url_fails_startup():
    with pytest.raises(RuntimeError):
        create_app(Settings(_env_file=None, app_env="test", database_url=""))


def test_concurrent_duplicate_deliveries_create_one_case(app, payload):
    """Race on the unique key: exactly one case, the rest are linked duplicates."""
    from concurrent.futures import ThreadPoolExecutor

    def send(_):
        return post_intake(TestClient(app), payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(8)))
    results = sorted(r.json()["result"] for r in responses)
    assert results == ["accepted"] + ["duplicate_ignored"] * 7
    assert len({r.json()["case_id"] for r in responses}) == 1
    assert count(app, IntakeEvent) == 1
    assert count(app, RawInboundDelivery) == 8
    assert count(app, RepairCase) == SEEDED_CASES + 1


def _pending_commands_for(app, client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    return case_id


def test_a_locked_command_is_skipped_not_waited_on(client, app, payload):
    from sqlalchemy import text

    _pending_commands_for(app, client, payload)  # creates two pending commands
    holder = app.state.sessionmaker()
    other = app.state.sessionmaker()
    try:
        # worker A has claimed the first command and has not finished yet
        claimed = holder.scalars(select(OutboxCommand).order_by(OutboxCommand.id).limit(1)
                                 .with_for_update()).first()
        assert claimed is not None
        other.execute(text("SET lock_timeout = '3s'"))  # a blocking worker would fail here
        assert process_pending(other) == 1  # only the other command; the locked one is skipped
        assert other.get(OutboxCommand, claimed.id).status == "PENDING"
    finally:
        holder.rollback()
        other.rollback()
    with app.state.sessionmaker() as s:
        assert process_pending(s) == 1  # worker A "crashed": the command is picked up later
        assert {c.status for c in s.scalars(select(OutboxCommand))} == {"DONE"}
    holder.close()
    other.close()


def test_two_workers_never_run_the_same_command_twice(client, app, payload):
    from concurrent.futures import ThreadPoolExecutor

    case_id = _pending_commands_for(app, client, payload)
    with app.state.sessionmaker() as s:
        s.add_all(OutboxCommand(command="CREATE_REVIEW_ITEM", case_id=case_id) for _ in range(40))
        s.commit()

    def work(_):
        with app.state.sessionmaker() as session:
            return process_pending(session)

    with ThreadPoolExecutor(max_workers=2) as pool:
        total = sum(pool.map(work, range(2)))
    assert total == 42  # 40 + the 2 created at intake, each processed exactly once
    with app.state.sessionmaker() as s:
        assert {c.status for c in s.scalars(select(OutboxCommand))} == {"DONE"}
        assert {c.attempts for c in s.scalars(select(OutboxCommand))} == {1}
        review_items = s.scalars(select(WorkItem).where(WorkItem.kind == "REVIEW_INTAKE")).all()
        assert len(review_items) == 1 + 40  # the intake item plus one per command, no duplicates


def test_batch_limit_caps_one_pass(client, app, payload):
    _pending_commands_for(app, client, payload)
    with app.state.sessionmaker() as s:
        assert process_pending(s, limit=1) == 1
        assert [c.status for c in s.scalars(select(OutboxCommand).order_by(OutboxCommand.id))] == [
            "DONE", "PENDING"]


def test_failure_audit_is_attached_to_the_case_when_it_exists(client, app, payload):
    case_id = _pending_commands_for(app, client, payload)
    with app.state.sessionmaker() as s:
        s.add(OutboxCommand(command="NO_SUCH_COMMAND", case_id=case_id))
        s.commit()
        process_pending(s)
        failed = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "WORKER_COMMAND_FAILED")).one()
        assert failed.case_id == case_id
    timeline = client.get(f"/api/cases/{case_id}/audit", headers=as_identity("disp-1")).json()
    assert "WORKER_COMMAND_FAILED" in [e["event_type"] for e in timeline]
