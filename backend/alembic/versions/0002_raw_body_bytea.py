"""store the raw delivery body as bytes

Revision ID: 0002
Revises: 0001
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "raw_inbound_delivery", "raw_body",
        existing_type=sa.Text(), type_=sa.LargeBinary(), existing_nullable=False,
        postgresql_using="convert_to(raw_body, 'UTF8')",
    )


def downgrade() -> None:
    # Lossy for bodies that are not valid UTF-8: they cannot be represented as text.
    op.alter_column(
        "raw_inbound_delivery", "raw_body",
        existing_type=sa.LargeBinary(), type_=sa.Text(), existing_nullable=False,
        postgresql_using="convert_from(raw_body, 'UTF8')",
    )
