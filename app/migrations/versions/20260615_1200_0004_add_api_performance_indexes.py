"""add API performance indexes

Revision ID: 0004_api_perf_indexes
Revises: 0003_scheduled_reports
Create Date: 2026-06-15 12:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_api_perf_indexes"
down_revision: str | None = "0003_scheduled_reports"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_org_created_at", "users", ["org_id", "created_at"])

    op.create_index(
        "ix_db_connections_org_created_at",
        "db_connections",
        ["org_id", "created_at"],
    )

    op.create_index("ix_dca_connection_id", "db_connection_access", ["connection_id"])
    op.create_index("ix_dca_org_user", "db_connection_access", ["org_id", "user_id"])
    op.create_index(
        "ix_dca_connection_org_user",
        "db_connection_access",
        ["connection_id", "org_id", "user_id"],
    )

    op.create_index(
        "ix_gi_sessions_org_user_created_at",
        "gi_sessions",
        ["org_id", "user_id", "created_at"],
    )

    op.create_index(
        "ix_query_history_user_created",
        "query_history",
        ["user_id", "created_at"],
    )

    op.create_index(
        "ix_reports_org_created_at",
        "reports",
        ["org_id", "created_at"],
    )
    op.create_index(
        "ix_reports_next_refresh_due",
        "reports",
        ["next_refresh_at", "refresh_interval_days"],
    )

    op.create_index(
        "ix_report_columns_report_position",
        "report_columns",
        ["report_id", "position"],
    )

    op.create_index(
        "ix_report_snapshots_report_created_at",
        "report_snapshots",
        ["report_id", "created_at"],
    )
    op.create_index(
        "ix_report_snapshots_org_created_at",
        "report_snapshots",
        ["org_id", "created_at"],
    )

    op.create_index(
        "ix_dashboards_org_created_at",
        "dashboards",
        ["org_id", "created_at"],
    )
    op.create_index(
        "ix_dashboard_reports_report_id",
        "dashboard_reports",
        ["report_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_dashboard_reports_report_id", table_name="dashboard_reports")
    op.drop_index("ix_dashboards_org_created_at", table_name="dashboards")
    op.drop_index("ix_report_snapshots_org_created_at", table_name="report_snapshots")
    op.drop_index("ix_report_snapshots_report_created_at", table_name="report_snapshots")
    op.drop_index("ix_report_columns_report_position", table_name="report_columns")
    op.drop_index("ix_reports_next_refresh_due", table_name="reports")
    op.drop_index("ix_reports_org_created_at", table_name="reports")
    op.drop_index("ix_gi_sessions_org_user_created_at", table_name="gi_sessions")
    op.drop_index("ix_query_history_user_created", table_name="query_history")
    op.drop_index("ix_dca_connection_org_user", table_name="db_connection_access")
    op.drop_index("ix_dca_org_user", table_name="db_connection_access")
    op.drop_index("ix_dca_connection_id", table_name="db_connection_access")
    op.drop_index("ix_db_connections_org_created_at", table_name="db_connections")
    op.drop_index("ix_users_org_created_at", table_name="users")
    op.drop_index("ix_users_email", table_name="users")
