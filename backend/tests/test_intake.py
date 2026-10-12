import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import (
    AuditEvent,
    IntakeEvent,
    RawInboundDelivery,
    RepairCase,
    SystemIssue,
    WorkItem,
)
from tests.conftest import SEEDED_AUDIT_EVENTS, SEEDED_CASES, as_identity, count, post_intake


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


def test_raw_body_is_stored_byte_for_byte(client, app, payload):
    odd = b'  {"b":1,\n "source_system":"website_form", "source_event_id":"e1",' \
          b' "idempotency_key":"k1"}  '
    assert post_intake(client, odd).status_code == 202
    with app.state.sessionmaker() as s:
        assert s.scalars(select(RawInboundDelivery)).one().raw_body == odd


@pytest.mark.parametrize("body", [
    b'{"source_system": "a\x00b"}',                 # raw NUL byte
    b'{"source_system": "caf\xe9"}',                # not valid UTF-8
    b"[" * 200000,                                  # nesting deep enough to hit the recursion limit
])
def test_hostile_bodies_are_kept_unchanged_and_rejected_with_400(client, app, body):
    r = post_intake(client, body)
    assert r.status_code == 400
    assert r.json() == {"error": "invalid_json"}
    with app.state.sessionmaker() as s:
        delivery = s.scalars(select(RawInboundDelivery)).one()
        assert delivery.raw_body == body
        assert delivery.parse_status == "INVALID_JSON"
    assert count(app, IntakeEvent) == 0


@pytest.mark.parametrize("raw_key", ["é".encode("latin-1"), "ключ".encode(), b"\xff\xfe"])
def test_non_ascii_key_header_is_401_not_500(app, raw_key):
    # httpx cannot send a non-ASCII header as str, so the raw header bytes are used.
    headers = [(b"content-type", b"application/json"), (b"x-intake-webhook-key", raw_key)]
    r = TestClient(app, raise_server_exceptions=False).post(
        "/webhooks/request_in", content=b"{}", headers=headers
    )
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert count(app, RawInboundDelivery) == 0



MARKER = "PII-MARKER-should-never-be-echoed"


@pytest.mark.parametrize("mutate,field", [
    (lambda p: p.update(location_type="RESIDENTIAL_APARTMENT"), "location_type"),
    (lambda p: p.update(location_type="residential"), "location_type"),
    (lambda p: p.update(source_system="x" * 200), "source_system"),
    (lambda p: p["contact"].update(display_name={"a": MARKER}), "contact.display_name"),
    (lambda p: p["contact"].update(phone=MARKER * 10), "contact.phone"),
    (lambda p: p.update(units="not-a-list"), "units"),
    (lambda p: p.update(units=[MARKER]), "units.0"),
    (lambda p: p["units"][0].update(power_source="NUCLEAR"), "units.0.power_source"),
    (lambda p: p.update(hazards=MARKER), "hazards"),
    (lambda p: p.update(notes=MARKER * 400), "notes"),
])
def test_invalid_fields_are_400_and_the_delivery_is_kept(client, app, payload, mutate, field):
    mutate(payload)
    r = post_intake(client, payload)
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_payload"
    assert field in r.json()["fields"]
    assert MARKER not in r.text  # values are never echoed back
    with app.state.sessionmaker() as s:
        delivery = s.scalars(select(RawInboundDelivery)).one()
        assert delivery.parse_status == "INVALID_PAYLOAD"
        rejected = s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "DELIVERY_REJECTED_INVALID")).one()
        assert MARKER not in rejected.detail
    assert count(app, IntakeEvent) == 0
    assert count(app, RepairCase) == SEEDED_CASES


def test_unknown_extra_fields_are_accepted(client, payload):
    payload["vendor_specific"] = {"anything": [1, 2, 3]}
    payload["units"][0]["serial_hint"] = "abc"
    assert post_intake(client, payload).status_code == 202


def test_unexpected_error_marks_the_delivery_failed_and_is_visible(client, app, payload,
                                                                    monkeypatch):
    import app.intake as intake

    def boom(*_args, **_kwargs):
        raise RuntimeError(f"database said: {MARKER}")

    monkeypatch.setattr(intake, "_find_event", boom)
    r = post_intake(client, payload)
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error"}
    with app.state.sessionmaker() as s:
        assert s.scalars(select(RawInboundDelivery)).one().parse_status == "FAILED"
        issue = s.scalars(select(SystemIssue)).one()
        assert issue.kind == "INTAKE_PROCESSING_FAILED"
        assert "RuntimeError" in issue.detail and MARKER not in issue.detail
        assert MARKER not in s.scalars(select(AuditEvent).where(
            AuditEvent.event_type == "DELIVERY_FAILED")).one().detail
    listed = client.get("/api/issues", headers=as_identity("owner-1")).json()
    assert [i["kind"] for i in listed] == ["INTAKE_PROCESSING_FAILED"]

    # the source retries once the fault is gone: the same event is then accepted normally
    monkeypatch.undo()
    assert post_intake(client, payload).json()["result"] == "accepted"
