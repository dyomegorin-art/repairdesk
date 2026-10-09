"""Outbox worker. Only safe internal commands; no external calls (ТЗ section 7)."""
import json
import logging
import time

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import (
    IntakeEvent,
    OutboxCommand,
    RawInboundDelivery,
    RepairCase,
    SystemIssue,
    WorkItem,
)
from app.rules import evaluate_eligibility, request_from_payload

ACTOR = "system:worker"
REQUIRED_FOR_READY = ("contact_display_name", "contact_phone", "location_type")


def _verify_intake_record(session: Session, case: RepairCase) -> dict:
    missing = [f for f in REQUIRED_FOR_READY if not getattr(case, f)]
    if case.units_json in ("", "[]"):
        missing.append("units")
    return {"missing_fields": missing}


def _create_review_item(session: Session, case: RepairCase) -> dict:
    session.add(WorkItem(case_id=case.case_id, kind="REVIEW_INTAKE"))
    return {"work_item": "REVIEW_INTAKE"}


def _evaluate_eligibility(session: Session, case: RepairCase) -> dict:
    """Advisory only: stores the result; never declines, sends or books anything."""
    event = session.get(IntakeEvent, case.intake_event_id) if case.intake_event_id else None
    if event is None:
        raise ValueError("case has no intake event")
    delivery = session.get(RawInboundDelivery, event.first_delivery_id)
    if delivery is None:
        raise ValueError("raw delivery not found")
    result = evaluate_eligibility(request_from_payload(json.loads(delivery.raw_body)))
    case.eligibility_result = result.result.value
    return {"eligibility": result.result.value, "reasons": result.codes}


HANDLERS = {
    "VERIFY_INTAKE_RECORD": _verify_intake_record,
    "CREATE_REVIEW_ITEM": _create_review_item,
    "EVALUATE_ELIGIBILITY": _evaluate_eligibility,
}


def _run(session: Session, cmd: OutboxCommand) -> dict:
    handler = HANDLERS.get(cmd.command)
    if handler is None:
        raise ValueError(f"unknown command {cmd.command}")
    case = session.get(RepairCase, cmd.case_id) if cmd.case_id else None
    if case is None:
        raise ValueError("case not found")
    return handler(session, case)


def process_pending(session: Session) -> int:
    commands = session.scalars(
        select(OutboxCommand).where(OutboxCommand.status == "PENDING").order_by(OutboxCommand.id)
    ).all()
    for cmd in commands:
        cmd.attempts += 1
        try:
            with session.begin_nested():
                result = _run(session, cmd)
                audit(session, "WORKER_COMMAND_COMPLETED", ACTOR, case_id=cmd.case_id,
                      command=cmd.command, **result)
            cmd.status = "DONE"
        except Exception as exc:  # noqa: BLE001 - a failure must become a visible issue
            cmd.status = "FAILED"
            cmd.error = str(exc)
            session.add(
                SystemIssue(kind="WORKER_COMMAND_FAILED", detail=str(exc), command_id=cmd.id)
            )
            audit(session, "WORKER_COMMAND_FAILED", ACTOR, command=cmd.command, error=str(exc))
        session.commit()
    return len(commands)


def main() -> None:
    from app.config import Settings
    from app.db import make_engine, make_sessionmaker

    settings = Settings()
    settings.validate_startup()
    factory = make_sessionmaker(make_engine(settings.database_url))
    while True:
        try:
            with factory() as session:
                process_pending(session)
        except SQLAlchemyError as exc:  # DB not ready or temporarily down: keep polling
            logging.getLogger("repairhub.worker").warning("poll failed: %s", type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
