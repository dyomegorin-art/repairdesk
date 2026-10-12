"""Intake pipeline: raw delivery -> identifiers -> idempotent event -> case.

Deterministic code only: business rules live in code (docs/SPEC.md, section 2.2).
"""
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import audit
from app.models import (
    IntakeEvent,
    OutboxCommand,
    RawInboundDelivery,
    RepairCase,
    WorkItem,
)

REQUIRED_IDENTIFIERS = ("source_system", "source_event_id", "idempotency_key")
SYSTEM_ACTOR = "system:ingress"


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
    audit(session, "DELIVERY_REJECTED_INVALID", SYSTEM_ACTOR,
          delivery_id=delivery.id, reason=status, **{k: v for k, v in body.items() if k == "field"})
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

    existing = _find_event(session, payload["source_system"], payload["idempotency_key"])
    if existing:
        return _duplicate(session, delivery, existing)

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
        return _duplicate(session, stored, existing)

    raw_contact = payload.get("contact")
    contact: dict = raw_contact if isinstance(raw_contact, dict) else {}
    raw_units = payload.get("units")
    units: list = raw_units if isinstance(raw_units, list) else []
    case = RepairCase(
        intake_event_id=event.id,
        source_attribution=payload["source_system"],
        contact_display_name=contact.get("display_name"),
        contact_phone=contact.get("phone"),
        location_type=payload.get("location_type"),
        units_json=json.dumps(units),
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
