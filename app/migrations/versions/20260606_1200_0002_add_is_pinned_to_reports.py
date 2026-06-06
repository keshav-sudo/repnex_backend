"""add is_pinned to reports

Revision ID: 0002_add_is_pinned
Revises: 0001_initial
Create Date: 2026-06-06 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_is_pinned"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reports",
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("reports", "is_pinned")
