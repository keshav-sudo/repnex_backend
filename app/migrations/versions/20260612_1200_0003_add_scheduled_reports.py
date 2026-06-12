"""add scheduled refresh + report_snapshots

Revision ID: 0003_scheduled_reports
Revises: 0002_add_is_pinned
Create Date: 2026-06-12 12:00:00
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_scheduled_reports"
down_revision: str | None = "0002_add_is_pinned"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── Add scheduled refresh columns to reports ──────────────────────────
    op.add_column(
        "reports",
        sa.Column("refresh_interval_days", sa.Integer(), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column("next_refresh_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "reports",
        sa.Column(
            "auto_refresh_connection_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_reports_auto_refresh_connection",
        "reports",
        "db_connections",
        ["auto_refresh_connection_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_reports_next_refresh_at",
        "reports",
        ["next_refresh_at"],
        postgresql_where=sa.text("next_refresh_at IS NOT NULL"),
    )

    # ── Create report_snapshots table ─────────────────────────────────────
    op.create_table(
        "report_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            default=uuid.uuid4,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "triggered_by",
            sa.String(32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "rows_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "rows_returned",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("execution_time_ms", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_report_snapshots_report_id",
        "report_snapshots",
        ["report_id"],
    )
    op.create_index(
        "ix_report_snapshots_org_id",
        "report_snapshots",
        ["org_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_snapshots_org_id", table_name="report_snapshots")
    op.drop_index("ix_report_snapshots_report_id", table_name="report_snapshots")
    op.drop_table("report_snapshots")

    op.drop_index("ix_reports_next_refresh_at", table_name="reports")
    op.drop_constraint(
        "fk_reports_auto_refresh_connection", "reports", type_="foreignkey"
    )
    op.drop_column("reports", "auto_refresh_connection_id")
    op.drop_column("reports", "last_refreshed_at")
    op.drop_column("reports", "next_refresh_at")
    op.drop_column("reports", "refresh_interval_days")
