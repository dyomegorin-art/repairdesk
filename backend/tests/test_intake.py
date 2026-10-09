import json
import logging

import pytest
from sqlalchemy import select

from app.models import (
    AuditEvent,
    IntakeEvent,
    RawInboundDelivery,
    RepairCase,
    WorkItem,
)
from tests.conftest import SEEDED_AUDIT_EVENTS, SEEDED_CASES, count, post_intake


def test_valid_intake_accepted_and_case_in_intake(client, app, payload):
    r = post_intake(client, payload)
    assert r.status_code == 202
    assert r.json()["result"] == "accepted"
    with app.state.sessionmaker() as s:
        case = s.get(RepairCase, r.json()["case_id"])
        assert case.case_phase == "INTAKE"
        assert case.intake_status == "NEW"
        assert s.scalars(select(WorkItem).where(WorkItem.case_id == case.case_id)).one()


def test_raw_delivery_stored_before_normalized_records(client, app, payload):
    r = post_intake(client, payload)
    case_id = r.json()["case_id"]
    with app.state.sessionmaker() as s:
        events = s.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
        types = [
            e.event_type for e in events
            if e.event_type != "CASE_CREATED" or e.case_id == case_id
        ]
        assert types.index("DELIVERY_RECEIVED") < types.index("INTAKE_EVENT_CREATED")
        event = s.scalars(select(IntakeEvent)).one()
        delivery = s.get(RawInboundDelivery, event.first_delivery_id)
        assert json.loads(delivery.raw_body)["source_event_id"] == payload["source_event_id"]


def test_duplicate_replay_is_ignored_and_linked(client, app, payload):
    first = post_intake(client, payload).json()
    second = post_intake(client, payload)
    assert second.status_code == 202
    assert second.json() == {"result": "duplicate_ignored", "case_id": first["case_id"]}
    assert count(app, RepairCase) == SEEDED_CASES + 1
    assert count(app, IntakeEvent) == 1
    assert count(app, RawInboundDelivery) == 2
    with app.state.sessionmaker() as s:
        dup = s.scalars(select(RawInboundDelivery).order_by(RawInboundDelivery.id)).all()[1]
        assert dup.duplicate_of_event_id == s.scalars(select(IntakeEvent)).one().id
        assert s.scalars(
            select(AuditEvent).where(AuditEvent.event_type == "DUPLICATE_IGNORED")
        ).one().case_id == first["case_id"]


def test_same_idempotency_key_different_source_is_new_event(client, app, payload):
    post_intake(client, payload)
    other = dict(payload, source_system="email_gateway")
    assert post_intake(client, other).json()["result"] == "accepted"
    assert count(app, IntakeEvent) == 2


def test_invalid_json_retains_delivery_only(client, app):
    r = post_intake(client, "{not json")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_json"
    assert count(app, RawInboundDelivery) == 1
    assert count(app, IntakeEvent) == 0
    assert count(app, RepairCase) == SEEDED_CASES


@pytest.mark.parametrize("field", ["source_system", "source_event_id", "idempotency_key"])
def test_each_missing_identifier_returns_400(client, app, payload, field):
    del payload[field]
    r = post_intake(client, payload)
    assert r.status_code == 400
    assert r.json() == {"error": "missing_identifier", "field": field}
    assert count(app, RawInboundDelivery) == 1
    assert count(app, IntakeEvent) == 0
    assert count(app, RepairCase) == SEEDED_CASES


@pytest.mark.parametrize("key", [None, "", "wrong-key"])
def test_bad_key_returns_401_and_retains_nothing(client, app, payload, key, caplog):
    payload["notes"] = "SECRET-BODY-MARKER"
    with caplog.at_level(logging.DEBUG):
        r = post_intake(client, payload, key=key)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert count(app, RawInboundDelivery) == 0
    assert count(app, IntakeEvent) == 0
    assert count(app, RepairCase) == SEEDED_CASES
    assert count(app, AuditEvent) == SEEDED_AUDIT_EVENTS  # seed events only
    assert "SECRET-BODY-MARKER" not in caplog.text
    assert "wrong-key" not in caplog.text


def test_unset_server_key_fails_closed(db_url, payload):
    from fastapi.testclient import TestClient

    from tests.conftest import build_app, make_settings

    app = build_app(make_settings(db_url, intake_webhook_key=""))
    r = post_intake(TestClient(app), payload, key="")
    assert r.status_code == 401
