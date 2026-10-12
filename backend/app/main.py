import hmac
import json
import logging
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import audit
from app.auth import (
    current_identity,
    deny,
    get_session,
    has_permission,
    permissions_for,
    require,
    require_any,
)
from app.config import Settings
from app.db import make_engine, make_sessionmaker
from app.intake import process_delivery, store_raw_delivery
from app.models import AuditEvent, Identity, RepairCase, SystemIssue

security_log = logging.getLogger("repairhub.security")

KEY_HEADER = "X-Intake-Webhook-Key"
ACTIVE_PHASES = ("ACCEPTED", "ACTIVE")


class IntakeStatusUpdate(BaseModel):
    intake_status: Literal["NEEDS_INFORMATION", "READY_FOR_REVIEW"]


def case_view(case: RepairCase) -> dict:
    return {
        "case_id": case.case_id,
        "case_phase": case.case_phase,
        "intake_status": case.intake_status,
        "eligibility_result": case.eligibility_result,
        "source_attribution": case.source_attribution,
        "assigned_technician_id": case.assigned_technician_id,
        "contact_display_name": case.contact_display_name,
        "location_type": case.location_type,
        "units": json.loads(case.units_json),
        "created_at": case.created_at.isoformat() if case.created_at else None,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.validate_startup()

    app = FastAPI(title="RepairHub Platform Frame", version="0.1.0")
    engine = make_engine(settings.database_url)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = make_sessionmaker(engine)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/me")
    def me(identity: Identity = Depends(current_identity)):
        # Informational only: the UI hides controls by this list, the backend still enforces.
        return {"identity_id": identity.identity_id, "role": identity.role,
                "permissions": permissions_for(identity.role)}

    @app.post("/webhooks/request_in")
    async def request_in(request: Request, session: Session = Depends(get_session)):
        supplied = request.headers.get(KEY_HEADER, "")
        expected = settings.intake_webhook_key
        # Fail closed: an unset server key rejects everything. Body is never read here.
        # Compare bytes: compare_digest raises TypeError on non-ASCII str, which would be a 500.
        if (
            not expected
            or not supplied
            or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        ):
            security_log.warning("webhook rejected: missing_or_invalid_key")
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        raw = await request.body()
        delivery = store_raw_delivery(session, raw)
        result = process_delivery(session, delivery)
        return JSONResponse(result.body, status_code=result.http_status)

    @app.get("/api/queues/new-requests")
    def new_requests(_=Depends(require("queue.view")), session: Session = Depends(get_session)):
        cases = session.scalars(
            select(RepairCase).where(RepairCase.case_phase == "INTAKE")
            .order_by(RepairCase.created_at)
        ).all()
        return [case_view(c) for c in cases]

    @app.get("/api/queues/engagements")
    def engagements(identity: Identity = Depends(require_any("queue.view", "queue.view_assigned")),
                    session: Session = Depends(get_session)):
        query = select(RepairCase).where(RepairCase.case_phase.in_(ACTIVE_PHASES))
        if not has_permission(identity, "queue.view"):
            # Technicians see only the jobs assigned to them.
            query = query.where(RepairCase.assigned_technician_id == identity.identity_id)
        cases = session.scalars(query.order_by(RepairCase.created_at)).all()
        return [case_view(c) for c in cases]

    def _get_case(session: Session, case_id: str) -> RepairCase:
        case = session.get(RepairCase, case_id)
        if case is None:
            raise HTTPException(404, "case_not_found")
        return case

    @app.get("/api/cases/{case_id}")
    def case_detail(case_id: str, request: Request,
                    identity: Identity = Depends(require_any("case.view", "case.view_assigned")),
                    session: Session = Depends(get_session)):
        if has_permission(identity, "case.view"):
            return case_view(_get_case(session, case_id))
        case = session.get(RepairCase, case_id)
        if (case is None or case.assigned_technician_id != identity.identity_id
                or case.case_phase not in ACTIVE_PHASES):
            deny(session, identity, request, "case.view_assigned",
                 case_id=case.case_id if case else None)
        return case_view(case)

    @app.patch("/api/cases/{case_id}/intake-status")
    def update_status(case_id: str, body: IntakeStatusUpdate,
                      identity: Identity = Depends(require("case.update_intake_status")),
                      session: Session = Depends(get_session)):
        case = _get_case(session, case_id)
        if case.case_phase != "INTAKE":
            raise HTTPException(409, "case_not_in_intake")
        previous = case.intake_status
        case.intake_status = body.intake_status
        audit(session, "INTAKE_STATUS_CHANGED", identity.identity_id, case_id=case_id,
              previous=previous, new=body.intake_status)
        session.commit()
        return case_view(case)

    @app.get("/api/cases/{case_id}/audit")
    def case_audit(case_id: str, _=Depends(require("audit.view")),
                   session: Session = Depends(get_session)):
        _get_case(session, case_id)
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.case_id == case_id).order_by(AuditEvent.id)
        ).all()
        return [
            {"id": e.id, "event_type": e.event_type, "actor": e.actor,
             "detail": json.loads(e.detail), "created_at": e.created_at.isoformat()}
            for e in events
        ]

    @app.get("/api/issues")
    def issues(_=Depends(require("issues.view")), session: Session = Depends(get_session)):
        rows = session.scalars(select(SystemIssue).order_by(SystemIssue.id)).all()
        return [{"id": r.id, "kind": r.kind, "detail": r.detail, "command_id": r.command_id}
                for r in rows]

    # Compiled PWA is served by the same service (docs/SPEC.md, 2.2).
    # Mounted last so API routes win.
    static_dir = Path(os.environ.get("STATIC_DIR", "static"))
    if static_dir.is_dir():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="pwa")

    return app
