"""add schema_info to connections

Revision ID: 0005_add_schema_info
Revises: 0004_api_perf_indexes
Create Date: 2026-06-16 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0005_add_schema_info"
down_revision: str | None = "0004_api_perf_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("db_connections", sa.Column("schema_info", JSONB, nullable=True))
    op.add_column("db_connections", sa.Column("schema_last_synced_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("db_connections", "schema_last_synced_at")
    op.drop_column("db_connections", "schema_info")
