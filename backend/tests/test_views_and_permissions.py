from sqlalchemy import select, update

from app.models import AuditEvent, RepairCase
from app.seed import SEEDED_ACCEPTED_CASE_ID, SEEDED_OTHER_TECH_CASE_ID
from tests.conftest import as_identity, count, post_intake


def test_dispatcher_sees_queue_and_detail(client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    queue = client.get("/api/queues/new-requests", headers=as_identity("disp-1"))
    assert queue.status_code == 200
    assert [c["case_id"] for c in queue.json()] == [case_id]
    detail = client.get(f"/api/cases/{case_id}", headers=as_identity("disp-1"))
    assert detail.status_code == 200
    assert detail.json()["case_phase"] == "INTAKE"
    assert detail.json()["units"][0]["appliance_type"] == "WASHER"


def test_dispatcher_marks_needs_information_and_ready_for_review(client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    for status in ("NEEDS_INFORMATION", "READY_FOR_REVIEW"):
        r = client.patch(f"/api/cases/{case_id}/intake-status",
                         json={"intake_status": status}, headers=as_identity("disp-1"))
        assert r.status_code == 200
        assert r.json()["intake_status"] == status


def test_status_outside_allowed_set_is_rejected(client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    r = client.patch(f"/api/cases/{case_id}/intake-status",
                     json={"intake_status": "READY_TO_MATCH"}, headers=as_identity("disp-1"))
    assert r.status_code == 422


def test_unauthorized_queue_access_rejected_and_audited(client, app):
    r = client.get("/api/queues/new-requests", headers=as_identity("tech-1"))
    assert r.status_code == 403
    with app.state.sessionmaker() as s:
        denied = s.scalars(select(AuditEvent).where(AuditEvent.event_type == "ACCESS_DENIED")).one()
        assert denied.actor == "tech-1"
        assert denied.case_id is None


def test_wrong_role_status_update_rejected_audited_and_unchanged(client, app, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    r = client.patch(f"/api/cases/{case_id}/intake-status",
                     json={"intake_status": "READY_FOR_REVIEW"}, headers=as_identity("dev-1"))
    assert r.status_code == 403
    with app.state.sessionmaker() as s:
        assert s.get(RepairCase, case_id).intake_status == "NEW"
        assert s.scalars(select(AuditEvent).where(AuditEvent.event_type == "ACCESS_DENIED")).one()


def test_audit_timeline_retrievable(client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    client.patch(f"/api/cases/{case_id}/intake-status",
                 json={"intake_status": "NEEDS_INFORMATION"}, headers=as_identity("disp-1"))
    r = client.get(f"/api/cases/{case_id}/audit", headers=as_identity("disp-1"))
    assert r.status_code == 200
    types = [e["event_type"] for e in r.json()]
    assert types == ["INTAKE_EVENT_CREATED", "CASE_CREATED", "WORK_ITEM_CREATED",
                     "INTAKE_STATUS_CHANGED"]
    assert r.json()[-1]["actor"] == "disp-1"


def test_seeded_accepted_case_shows_in_engagements_with_same_case_id(client, app, payload):
    eng = client.get("/api/queues/engagements", headers=as_identity("disp-1")).json()
    assert {c["case_id"] for c in eng} == {SEEDED_ACCEPTED_CASE_ID, SEEDED_OTHER_TECH_CASE_ID}
    detail = client.get(f"/api/cases/{SEEDED_ACCEPTED_CASE_ID}", headers=as_identity("disp-1"))
    assert detail.json()["case_id"] == SEEDED_ACCEPTED_CASE_ID
    assert client.get("/api/queues/new-requests", headers=as_identity("disp-1")).json() == []


def test_lead_becomes_engagement_without_copy(client, app, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    before = count(app, RepairCase)
    with app.state.sessionmaker() as s:
        s.execute(update(RepairCase).where(RepairCase.case_id == case_id)
                  .values(case_phase="ACCEPTED"))
        s.commit()
    eng_ids = [c["case_id"] for c in
               client.get("/api/queues/engagements", headers=as_identity("disp-1")).json()]
    assert case_id in eng_ids
    assert case_id not in [c["case_id"] for c in
                           client.get("/api/queues/new-requests",
                                      headers=as_identity("disp-1")).json()]
    assert count(app, RepairCase) == before


def test_me_returns_server_side_role_and_permissions(client):
    r = client.get("/api/me", headers={**as_identity("tech-1"), "X-Role": "owner"})
    assert r.json() == {"identity_id": "tech-1", "role": "technician",
                        "permissions": ["case.view_assigned", "queue.view_assigned"]}
    r = client.get("/api/me", headers=as_identity("disp-1"))
    assert "case.update_intake_status" in r.json()["permissions"]
    assert client.get("/api/me").status_code == 401


# --- technician: assigned-only access -------------------------------------------------

def test_technician_sees_only_own_engagements(client):
    own = client.get("/api/queues/engagements", headers=as_identity("tech-1")).json()
    assert [c["case_id"] for c in own] == [SEEDED_ACCEPTED_CASE_ID]
    other = client.get("/api/queues/engagements", headers=as_identity("tech-2")).json()
    assert [c["case_id"] for c in other] == [SEEDED_OTHER_TECH_CASE_ID]


def test_technician_opens_own_case_but_not_anothers_and_denial_is_audited(client, app):
    ok = client.get(f"/api/cases/{SEEDED_ACCEPTED_CASE_ID}", headers=as_identity("tech-1"))
    assert ok.status_code == 200
    assert ok.json()["assigned_technician_id"] == "tech-1"
    denied = client.get(f"/api/cases/{SEEDED_OTHER_TECH_CASE_ID}", headers=as_identity("tech-1"))
    assert denied.status_code == 403
    with app.state.sessionmaker() as s:
        ev = s.scalars(select(AuditEvent).where(AuditEvent.event_type == "ACCESS_DENIED")).one()
        assert ev.actor == "tech-1"
        assert ev.case_id == SEEDED_OTHER_TECH_CASE_ID


def test_technician_cannot_probe_for_case_existence(client):
    r = client.get("/api/cases/no-such-case", headers=as_identity("tech-1"))
    assert r.status_code == 403  # not 404: ids of other cases must not be discoverable


def test_technician_has_no_access_to_new_requests_audit_or_status_changes(client, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    t = as_identity("tech-1")
    assert client.get("/api/queues/new-requests", headers=t).status_code == 403
    assert client.get(f"/api/cases/{case_id}/audit", headers=t).status_code == 403
    assert client.patch(f"/api/cases/{case_id}/intake-status",
                        json={"intake_status": "READY_FOR_REVIEW"}, headers=t).status_code == 403


def test_assigned_technician_cannot_open_case_still_in_intake(client, app, payload):
    case_id = post_intake(client, payload).json()["case_id"]
    with app.state.sessionmaker() as s:
        s.execute(update(RepairCase).where(RepairCase.case_id == case_id)
                  .values(assigned_technician_id="tech-1"))
        s.commit()
    assert client.get(f"/api/cases/{case_id}", headers=as_identity("tech-1")).status_code == 403
