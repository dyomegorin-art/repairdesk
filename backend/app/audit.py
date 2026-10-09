import json

from sqlalchemy.orm import Session

from app.models import AuditEvent


def audit(
    session: Session,
    event_type: str,
    actor: str,
    case_id: str | None = None,
    **detail,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        actor=actor,
        case_id=case_id,
        detail=json.dumps(detail, sort_keys=True, default=str),
    )
    session.add(event)
    session.flush()
    return event
