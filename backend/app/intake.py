"""Intake pipeline: raw delivery -> identifiers -> idempotent event -> case.

Deterministic code only: business rules live in code (docs/SPEC.md, section 2.2).
"""
import hashlib
import json
import logging
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from app.schemas import IntakePayload

REQUIRED_IDENTIFIERS = ("source_system", "source_event_id", "idempotency_key")
SYSTEM_ACTOR = "system:ingress"
log = logging.getLogger("repairdesk.intake")


@dataclass
class IntakeResult:
    http_status: int
    body: dict


def store_raw_delivery(session: Session, raw: bytes) -> RawInboundDelivery:
    """Persist an authenticated delivery before anything is parsed (docs/SPEC.md, 2.3)."""
    delivery = RawInboundDelivery(raw_body=raw)
    session.add(delivery)
    session.flush()
    audit(session, "DELIVERY_RECEIVED", SYSTEM_ACTOR, delivery_id=delivery.id)
    session.commit()
    return delivery


def _reject(session: Session, delivery: RawInboundDelivery, status: str, body: dict):
    delivery.parse_status = status
    detail = {k: v for k, v in body.items() if k in ("field", "fields")}
    audit(session, "DELIVERY_REJECTED_INVALID", SYSTEM_ACTOR,
          delivery_id=delivery.id, reason=status, **detail)
    session.commit()
    return IntakeResult(400, body)


def _find_event(session: Session, source_system: str, key: str) -> IntakeEvent | None:
    return session.scalar(
        select(IntakeEvent).where(
            IntakeEvent.source_system == source_system,
            IntakeEvent.idempotency_key == key,
        )
    )


def _case_for_event(session: Session, event: IntakeEvent) -> RepairCase:
    return session.scalars(
        select(RepairCase).where(RepairCase.intake_event_id == event.id)
    ).one()


def payload_fingerprint(raw: bytes) -> str:
    """Hash of the canonical JSON, so whitespace and key order do not matter."""
    canonical = json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_existing(session: Session, delivery: RawInboundDelivery, event: IntakeEvent):
    """The key is already known: the same content is a harmless duplicate, different content
    under the same key is a conflict that must not be silently swallowed."""
    first = session.get(RawInboundDelivery, event.first_delivery_id)
    if first is not None and payload_fingerprint(first.raw_body) != payload_fingerprint(
        delivery.raw_body
    ):
        return _conflict(session, delivery, event)
    return _duplicate(session, delivery, event)


def _conflict(session: Session, delivery: RawInboundDelivery, event: IntakeEvent):
    case = _case_for_event(session, event)
    delivery.parse_status = "CONFLICT"
    delivery.intake_event_id = event.id
    audit(session, "IDEMPOTENCY_CONFLICT", SYSTEM_ACTOR, case_id=case.case_id,
          delivery_id=delivery.id, intake_event_id=event.id)
    session.add(SystemIssue(
        kind="IDEMPOTENCY_CONFLICT",
        detail=f"delivery {delivery.id} reuses the key of event {event.id} with different content",
    ))
    session.commit()
    return IntakeResult(409, {"error": "idempotency_conflict", "case_id": case.case_id})


def _duplicate(session: Session, delivery: RawInboundDelivery, event: IntakeEvent):
    case = _case_for_event(session, event)
    delivery.parse_status = "OK"
    delivery.intake_event_id = event.id
    delivery.duplicate_of_event_id = event.id
    audit(session, "DUPLICATE_IGNORED", SYSTEM_ACTOR, case_id=case.case_id,
          delivery_id=delivery.id, intake_event_id=event.id)
    session.commit()
    return IntakeResult(202, {"result": "duplicate_ignored", "case_id": case.case_id})


def process_delivery(session: Session, delivery: RawInboundDelivery) -> IntakeResult:
    try:
        payload = json.loads(delivery.raw_body)
    except (ValueError, RecursionError):  # includes invalid UTF-8 and too-deep nesting
        return _reject(session, delivery, "INVALID_JSON", {"error": "invalid_json"})
    if not isinstance(payload, dict):
        return _reject(session, delivery, "INVALID_JSON", {"error": "invalid_json"})

    for field in REQUIRED_IDENTIFIERS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            return _reject(
                session, delivery, "MISSING_IDENTIFIER",
                {"error": "missing_identifier", "field": field},
            )

    try:
        parsed = IntakePayload.model_validate(payload)
    except ValidationError as exc:
        # Report only where the problem is, never the submitted values (they may be PII).
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return _reject(session, delivery, "INVALID_PAYLOAD",
                       {"error": "invalid_payload", "fields": fields})

    existing = _find_event(session, payload["source_system"], payload["idempotency_key"])
    if existing:
        return _resolve_existing(session, delivery, existing)

    event = IntakeEvent(
        source_system=payload["source_system"],
        source_event_id=payload["source_event_id"],
        idempotency_key=payload["idempotency_key"],
        source_channel=payload.get("source_channel"),
        first_delivery_id=delivery.id,
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent delivery won the race on the unique key: treat as duplicate.
        session.rollback()
        stored = session.get(RawInboundDelivery, delivery.id)
        existing = _find_event(session, payload["source_system"], payload["idempotency_key"])
        if stored is None or existing is None:
            raise  # a unique violation without a winner would be a real bug: surface it
        return _resolve_existing(session, stored, existing)

    case = RepairCase(
        intake_event_id=event.id,
        source_attribution=parsed.source_system,
        contact_display_name=parsed.contact.display_name if parsed.contact else None,
        contact_phone=parsed.contact.phone if parsed.contact else None,
        location_type=parsed.location_type,
        units_json=json.dumps([u.model_dump(exclude_none=True) for u in parsed.units or []]),
    )
    session.add(case)
    session.flush()

    delivery.parse_status = "OK"
    delivery.intake_event_id = event.id
    session.add(WorkItem(case_id=case.case_id, kind="REVIEW_INTAKE"))
    session.add(OutboxCommand(command="VERIFY_INTAKE_RECORD", case_id=case.case_id))
    session.add(OutboxCommand(command="EVALUATE_ELIGIBILITY", case_id=case.case_id))

    audit(session, "INTAKE_EVENT_CREATED", SYSTEM_ACTOR, case_id=case.case_id,
          intake_event_id=event.id, delivery_id=delivery.id)
    audit(session, "CASE_CREATED", SYSTEM_ACTOR, case_id=case.case_id, case_phase="INTAKE")
    audit(session, "WORK_ITEM_CREATED", SYSTEM_ACTOR, case_id=case.case_id, kind="REVIEW_INTAKE")
    session.commit()
    return IntakeResult(202, {"result": "accepted", "case_id": case.case_id})


def process_delivery_safely(session: Session, delivery_id: int) -> IntakeResult:
    """Run the pipeline; an unexpected error must never leave a delivery stuck in PENDING.

    The raw delivery is already committed, so on failure it is marked FAILED, a visible
    SystemIssue is created and the source gets a generic 500 (and may retry). Only the
    exception class is recorded: messages from the database can contain submitted values.
    """
    delivery = session.get(RawInboundDelivery, delivery_id)
    if delivery is None:
        raise RuntimeError("delivery vanished after being stored")
    try:
        return process_delivery(session, delivery)
    except Exception as exc:  # noqa: BLE001 - last line of defence for the ingress path
        session.rollback()
        name = type(exc).__name__
        log.error("intake processing failed: %s (delivery %s)", name, delivery_id)
        failed = session.get(RawInboundDelivery, delivery_id)
        if failed is not None:
            failed.parse_status = "FAILED"
        session.add(SystemIssue(kind="INTAKE_PROCESSING_FAILED",
                                detail=f"delivery {delivery_id}: {name}"))
        audit(session, "DELIVERY_FAILED", SYSTEM_ACTOR, delivery_id=delivery_id, error=name)
        session.commit()
        return IntakeResult(500, {"error": "internal_error"})
