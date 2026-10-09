import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.config import Settings
from app.db import Base
from app.main import create_app
from app.seed import seed

KEY = "test-webhook-key"
SEEDED_CASES = 2  # seed creates two ACCEPTED cases (one per technician)
SEEDED_AUDIT_EVENTS = 2
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


def pytest_configure(config):
    # Tests run on PostgreSQL only: SQLite hides real behaviour (constraints, types, ALTER).
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.exit(
            "TEST_DATABASE_URL is required (PostgreSQL server with CREATE DATABASE rights). "
            "Run ../scripts/test.sh to start a throwaway one.",
            returncode=2,
        )


@pytest.fixture
def db_url():
    """A fresh, empty PostgreSQL database per test; dropped afterwards."""
    base = make_url(os.environ["TEST_DATABASE_URL"])
    name = f"test_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def make_settings(db_url, **overrides) -> Settings:
    values = dict(
        app_env="test",
        allow_local_synthetic_auth=True,
        intake_webhook_key=KEY,
        database_url=db_url,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


def build_app(settings: Settings, create_schema: bool = True):
    app = create_app(settings)
    if create_schema:
        Base.metadata.create_all(app.state.engine)
        with app.state.sessionmaker() as session:
            seed(session)
    return app


@pytest.fixture
def settings(db_url):
    return make_settings(db_url)


@pytest.fixture
def app(settings):
    return build_app(settings)


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def payload():
    return json.loads((FIXTURES / "request_valid.json").read_text())


def post_intake(client, body, key=KEY):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["X-Intake-Webhook-Key"] = key
    data = body if isinstance(body, (str, bytes)) else json.dumps(body)
    return client.post("/webhooks/request_in", content=data, headers=headers)


def as_identity(identity_id):
    return {"X-Synthetic-Identity": identity_id}


def count(app, model):
    from sqlalchemy import func, select

    with app.state.sessionmaker() as s:
        return s.scalar(select(func.count()).select_from(model))
