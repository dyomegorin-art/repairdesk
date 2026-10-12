"""indexes for the columns the queues, the audit timeline and the worker filter on

PostgreSQL does not index foreign keys automatically.

Revision ID: 0003
Revises: 0002
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = (
    ("ix_repair_case_case_phase", "repair_case", ["case_phase"]),
    ("ix_repair_case_assigned_technician_id", "repair_case", ["assigned_technician_id"]),
    ("ix_audit_event_case_id", "audit_event", ["case_id"]),
    ("ix_outbox_command_status", "outbox_command", ["status"]),
)


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
