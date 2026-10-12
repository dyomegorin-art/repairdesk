import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id() -> str:
    return str(uuid.uuid4())


class Identity(Base):
    """Pre-seeded synthetic identity; the role comes from here, never from the client."""

    __tablename__ = "identity"

    identity_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))


class RawInboundDelivery(Base):
    __tablename__ = "raw_inbound_delivery"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    # Exact bytes as received: never decoded or altered before parsing.
    raw_body: Mapped[bytes] = mapped_column(LargeBinary)
    parse_status: Mapped[str] = mapped_column(String(32), default="PENDING")
    # use_alter: raw_inbound_delivery and intake_event reference each other.
    intake_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_event.id", use_alter=True, name="fk_delivery_event"),
        nullable=True,
    )
    duplicate_of_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_event.id", use_alter=True, name="fk_delivery_duplicate_of"),
        nullable=True,
    )


class IntakeEvent(Base):
    __tablename__ = "intake_event"
    __table_args__ = (
        UniqueConstraint("source_system", "idempotency_key", name="uq_intake_dedup"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_system: Mapped[str] = mapped_column(String(128))
    source_event_id: Mapped[str] = mapped_column(String(256))
    idempotency_key: Mapped[str] = mapped_column(String(256))
    source_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    first_delivery_id: Mapped[int] = mapped_column(ForeignKey("raw_inbound_delivery.id"))


class RepairCase(Base):
    __tablename__ = "repair_case"

    case_id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    case_phase: Mapped[str] = mapped_column(String(32), default="INTAKE")
    intake_status: Mapped[str] = mapped_column(String(32), default="NEW")
    eligibility_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    intake_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("intake_event.id"), nullable=True, unique=True
    )
    source_attribution: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Set by the (future) assignment workflow; in v0.1 only by seed/DB.
    assigned_technician_id: Mapped[str | None] = mapped_column(
        ForeignKey("identity.identity_id", name="fk_case_assigned_technician"), nullable=True
    )
    contact_display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    location_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    units_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkItem(Base):
    __tablename__ = "work_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str] = mapped_column(ForeignKey("repair_case.case_id"))
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("repair_case.case_id"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class OutboxCommand(Base):
    __tablename__ = "outbox_command"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command: Mapped[str] = mapped_column(String(64))
    case_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemIssue(Base):
    __tablename__ = "system_issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text)
    command_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
