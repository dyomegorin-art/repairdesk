import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select

from app.config import Settings
from app.main import create_app
from app.models import (
    AuditEvent,
    IntakeEvent,
    OutboxCommand,
    RawInboundDelivery,
    RepairCase,
)
from app.worker import process_pending
from tests.conftest import (
    SEEDED_CASES,
    as_identity,
    build_app,
    count,
    make_settings,
    post_intake,
)


def test_worker_completes_safe_command_and_audits(client, app, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    with app.state.sessionmaker() as s:
        assert process_pending(s) == 1
        cmd = s.scalars(select(OutboxCommand)).one()
        assert cmd.status == "DONE"
        done = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "WORKER_COMMAND_COMPLETED")).one()
        assert done.case_id == case_id
        assert '"missing_fields": []' in done.detail


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
    backend = Path(__file__).resolve().parents[1]
    env = {"DATABASE_URL": db_url, "PATH": "/usr/bin:/usr/local/bin"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=backend, env=env, check=True, capture_output=True)
    from app.db import Base
    engine = create_engine(db_url)
    insp = inspect(engine)
    assert set(insp.get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
    for name, table in Base.metadata.tables.items():
        assert {c["name"] for c in insp.get_columns(name)} == {c.name for c in table.columns}, name
    # the circular foreign keys exist on PostgreSQL
    fks = {fk["name"] for fk in insp.get_foreign_keys("raw_inbound_delivery")}
    assert {"fk_delivery_event", "fk_delivery_duplicate_of"} <= fks
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
