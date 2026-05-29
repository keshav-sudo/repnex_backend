"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2025-01-01 00:00:00
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    plan_type = postgresql.ENUM("free", "pro", "enterprise", name="plan_type", create_type=False)
    user_role = postgresql.ENUM("admin", "editor", "viewer", name="user_role", create_type=False)
    user_status = postgresql.ENUM(
        "pending", "active", "expired", name="user_status", create_type=False
    )
    db_type = postgresql.ENUM(
        "postgres", "mysql", "mssql", "oracle", "cloudsql", name="db_type", create_type=False
    )
    session_status = postgresql.ENUM(
        "active", "archived", name="session_status", create_type=False
    )
    exec_status = postgresql.ENUM(
        "success", "error", "rate_limited", name="execution_status", create_type=False
    )

    bind = op.get_bind()
    # for e in (plan_type, user_role, user_status, db_type, session_status, exec_status):
    #     try:
    #         e.create(bind, checkfirst=True)
    #     except Exception:
    #         pass

    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False, unique=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "plan_type",
            sa.Enum("free", "pro", "enterprise", name="plan_type", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=True),
        sa.Column(
            "role",
            sa.Enum("admin", "editor", "viewer", name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.Enum("pending", "active", "expired", name="user_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("org_id", "email", name="uq_users_org_email"),
    )
    op.create_index("ix_users_org_id", "users", ["org_id"])

    op.create_foreign_key(
        "fk_organizations_owner_id_users",
        "organizations",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "db_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "db_type",
            sa.Enum(
                "postgres", "mysql", "mssql", "oracle", "cloudsql",
                name="db_type", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("port", sa.Integer, nullable=False),
        sa.Column("db_name", sa.String(255), nullable=False),
        sa.Column("encrypted_username", sa.Text, nullable=False),
        sa.Column("encrypted_password", sa.Text, nullable=False),
        sa.Column("ssl_enabled", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_db_connections_org_id", "db_connections", ["org_id"])

    op.create_table(
        "db_connection_access",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("connection_id", "user_id", name="uq_dca_conn_user"),
    )

    op.create_table(
        "gi_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("context_window", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum("active", "archived", name="session_status", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_gi_sessions_user_id", "gi_sessions", ["user_id"])

    op.create_table(
        "query_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("gi_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("db_connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("natural_language_input", sa.Text, nullable=False),
        sa.Column("generated_sql", sa.Text, nullable=True),
        sa.Column("row_size", sa.Boolean, nullable=True, server_default=sa.text("false")),
        sa.Column("intent", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "execution_status",
            sa.Enum(
                "success", "error", "rate_limited",
                name="execution_status", create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("execution_time_ms", sa.Integer, nullable=True),
        sa.Column("rows_returned", sa.Integer, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_query_history_session_id", "query_history", ["session_id"])

    op.create_table(
        "reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("query_template_id", sa.String(128), nullable=False),
        sa.Column("parameters", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("is_public", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_reports_org_id", "reports", ["org_id"])

    op.create_table(
        "report_columns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("column_name", sa.String(128), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("is_visible", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("data_type", sa.String(32), nullable=False),
        sa.Column("format_config", postgresql.JSONB, nullable=False, server_default="{}"),
    )

    op.create_table(
        "dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("layout_config", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_dashboards_org_id", "dashboards", ["org_id"])

    op.create_table(
        "dashboard_reports",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dashboard_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position_x", sa.Integer, nullable=False, server_default="0"),
        sa.Column("position_y", sa.Integer, nullable=False, server_default="0"),
        sa.Column("width", sa.Integer, nullable=False, server_default="4"),
        sa.Column("height", sa.Integer, nullable=False, server_default="4"),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("dashboard_id", "report_id", name="uq_dr_dash_report"),
    )


def downgrade() -> None:
    op.drop_table("dashboard_reports")
    op.drop_index("ix_dashboards_org_id", table_name="dashboards")
    op.drop_table("dashboards")
    op.drop_table("report_columns")
    op.drop_index("ix_reports_org_id", table_name="reports")
    op.drop_table("reports")
    op.drop_index("ix_query_history_session_id", table_name="query_history")
    op.drop_table("query_history")
    op.drop_index("ix_gi_sessions_user_id", table_name="gi_sessions")
    op.drop_table("gi_sessions")
    op.drop_table("db_connection_access")
    op.drop_index("ix_db_connections_org_id", table_name="db_connections")
    op.drop_table("db_connections")
    op.drop_constraint("fk_organizations_owner_id_users", "organizations", type_="foreignkey")
    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
    op.drop_table("organizations")
    for name in (
        "execution_status",
        "session_status",
        "db_type",
        "user_status",
        "user_role",
        "plan_type",
    ):
        op.execute(f"DROP TYPE IF EXISTS {name}")
