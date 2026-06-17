"""add hide_sql_queries to organizations

Revision ID: 0006_add_hide_sql_queries
Revises: 0005_add_schema_info
Create Date: 2026-06-17 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_hide_sql_queries"
down_revision: str | None = "0005_add_schema_info"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("hide_sql_queries", sa.Boolean(), nullable=False, server_default="false"))


def downgrade() -> None:
    op.drop_column("organizations", "hide_sql_queries")
