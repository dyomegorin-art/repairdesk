"""Outbox worker. Only safe internal commands; no external calls (ТЗ section 7)."""
import json
import logging
import time
from datetime import datetime, timedelta

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
    utcnow,
)
from app.rules import evaluate_eligibility, request_from_payload

ACTOR = "system:worker"


class PermanentCommandError(Exception):
    """A deterministic failure: running the command again cannot help, so it is not retried."""
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
        raise PermanentCommandError("case has no intake event")
    delivery = session.get(RawInboundDelivery, event.first_delivery_id)
    if delivery is None:
        raise PermanentCommandError("raw delivery not found")
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
        raise PermanentCommandError(f"unknown command {cmd.command}")
    case = session.get(RepairCase, cmd.case_id) if cmd.case_id else None
    if case is None:
        raise PermanentCommandError("case not found")
    return handler(session, case)


BATCH_LIMIT = 100
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 15 * 60


def backoff_seconds(attempt: int) -> int:
    """Pause after the `attempt`-th failed try: 30, 60, 120, 240, ... capped at 15 minutes."""
    return min(BACKOFF_BASE_SECONDS * 2 ** (attempt - 1), BACKOFF_CAP_SECONDS)


def _claim_next(session: Session, now: datetime) -> OutboxCommand | None:
    """Lock one due command. SKIP LOCKED lets several workers run side by side:
    a command another worker is processing is simply passed over."""
    return session.scalars(
        select(OutboxCommand)
        .where(OutboxCommand.status == "PENDING", OutboxCommand.next_attempt_at <= now)
        .order_by(OutboxCommand.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).first()


def _record_failure(session: Session, cmd: OutboxCommand, exc: Exception, now: datetime) -> None:
    permanent = isinstance(exc, PermanentCommandError)
    # Our own messages are safe to store; for anything else keep only the class name,
    # because database errors can contain submitted values.
    error = str(exc) if permanent else type(exc).__name__
    cmd.error = error
    # Attach the event to the case timeline when the case exists (FK safe).
    case_id = cmd.case_id if cmd.case_id and session.get(RepairCase, cmd.case_id) else None

    if not permanent and cmd.attempts < MAX_ATTEMPTS:
        delay = backoff_seconds(cmd.attempts)
        cmd.next_attempt_at = now + timedelta(seconds=delay)
        audit(session, "WORKER_COMMAND_RETRY", ACTOR, case_id=case_id, command=cmd.command,
              attempt=cmd.attempts, retry_in_seconds=delay, error=error)
        return

    cmd.status = "FAILED"
    session.add(SystemIssue(
        kind="WORKER_COMMAND_FAILED", command_id=cmd.id,
        detail=f"{cmd.command} failed after {cmd.attempts} attempt(s): {error}",
    ))
    audit(session, "WORKER_COMMAND_FAILED", ACTOR, case_id=case_id, command=cmd.command,
          attempts=cmd.attempts, error=error)


def process_pending(
    session: Session, limit: int = BATCH_LIMIT, now: datetime | None = None
) -> int:
    """Process up to `limit` due commands, one at a time: claim, run, commit (releases the lock).

    A transient failure reschedules the command with exponential backoff until MAX_ATTEMPTS;
    a permanent one, or the last failed attempt, makes it FAILED with a visible SystemIssue.
    `now` is injectable so the schedule can be tested without sleeping.
    """
    now = now or utcnow()
    processed = 0
    while processed < limit:
        cmd = _claim_next(session, now)
        if cmd is None:
            break
        cmd.attempts += 1
        try:
            with session.begin_nested():
                result = _run(session, cmd)
                audit(session, "WORKER_COMMAND_COMPLETED", ACTOR, case_id=cmd.case_id,
                      command=cmd.command, **result)
            cmd.status = "DONE"
        except Exception as exc:  # noqa: BLE001 - every failure is retried or made visible
            _record_failure(session, cmd, exc, now)
        session.commit()
        processed += 1
    return processed


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
            logging.getLogger("repairdesk.worker").warning("poll failed: %s", type(exc).__name__)
        time.sleep(2)


if __name__ == "__main__":
    main()
