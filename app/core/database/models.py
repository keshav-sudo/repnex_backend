from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base


class PlanType(str, enum.Enum):
    free = "free"
    pro = "pro"
    enterprise = "enterprise"


class UserRole(str, enum.Enum):
    admin = "admin"
    editor = "editor"
    viewer = "viewer"


class UserStatus(str, enum.Enum):
    pending = "pending"
    active = "active"
    expired = "expired"


class DBType(str, enum.Enum):
    postgres = "postgres"
    mysql = "mysql"
    mssql = "mssql"
    oracle = "oracle"
    cloudsql = "cloudsql"


class SessionStatus(str, enum.Enum):
    active = "active"
    archived = "archived"


class ExecutionStatus(str, enum.Enum):
    success = "success"
    error = "error"
    rate_limited = "rate_limited"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts_created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


# ──────────────────────────────────────────────────────────────────────────────
class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", use_alter=True, ondelete="SET NULL")
    )
    plan_type: Mapped[PlanType] = mapped_column(
        Enum(PlanType, name="plan_type"), default=PlanType.free, nullable=False
    )
    hide_sql_queries: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = _ts_created()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("org_id", "email", name="uq_users_org_email"),
        Index("ix_users_email", "email"),
        Index("ix_users_org_id", "org_id"),
        Index("ix_users_org_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.viewer, nullable=False
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), default=UserStatus.pending, nullable=False
    )
    created_at: Mapped[datetime] = _ts_created()


class DBConnection(Base):
    __tablename__ = "db_connections"
    __table_args__ = (
        Index("ix_db_connections_org_id", "org_id"),
        Index("ix_db_connections_org_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    db_type: Mapped[DBType] = mapped_column(Enum(DBType, name="db_type"), nullable=False)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    db_name: Mapped[str] = mapped_column(String(255), nullable=False)
    encrypted_username: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_password: Mapped[str] = mapped_column(Text, nullable=False)
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    schema_info: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    schema_last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _ts_created()


class DBConnectionAccess(Base):
    __tablename__ = "db_connection_access"
    __table_args__ = (
        UniqueConstraint("connection_id", "user_id", name="uq_dca_conn_user"),
        Index("ix_dca_connection_id", "connection_id"),
        Index("ix_dca_connection_org_user", "connection_id", "org_id", "user_id"),
        Index("ix_dca_org_user", "org_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    granted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = _ts_created()


class GISession(Base):
    __tablename__ = "gi_sessions"
    __table_args__ = (
        Index("ix_gi_sessions_user_id", "user_id"),
        Index("ix_gi_sessions_org_user_created_at", "org_id", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    context_window: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        Enum(SessionStatus, name="session_status"), default=SessionStatus.active, nullable=False
    )
    created_at: Mapped[datetime] = _ts_created()


class QueryHistory(Base):
    __tablename__ = "query_history"
    __table_args__ = (
        Index("ix_query_history_session_id", "session_id"),
        Index("ix_query_history_user_created", "user_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("gi_sessions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("db_connections.id", ondelete="CASCADE"), nullable=False
    )
    natural_language_input: Mapped[str] = mapped_column(Text, nullable=False)
    generated_sql: Mapped[str | None] = mapped_column(Text)
    row_size: Mapped[bool | None] = mapped_column(Boolean, default=False)
    intent: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    execution_status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus, name="execution_status"), nullable=False
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer)
    rows_returned: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _ts_created()


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        Index("ix_reports_org_id", "org_id"),
        Index("ix_reports_org_created_at", "org_id", "created_at"),
        Index("ix_reports_next_refresh_due", "next_refresh_at", "refresh_interval_days"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    query_template_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parameters: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # ── Scheduled auto-refresh fields ──────────────────────────────────────
    # refresh_interval_days: None/0 = manual only, 1 = daily, 2 = every 2d, 3 = every 3d
    refresh_interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_refresh_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auto_refresh_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("db_connections.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = _ts_created()

    columns: Mapped[list["ReportColumn"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="selectin"
    )
    snapshots: Mapped[list["ReportSnapshot"]] = relationship(
        back_populates="report", cascade="all, delete-orphan", lazy="noload"
    )


class ReportColumn(Base):
    __tablename__ = "report_columns"
    __table_args__ = (Index("ix_report_columns_report_position", "report_id", "position"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    column_name: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    format_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    report: Mapped[Report] = relationship(back_populates="columns")


# ── Report Snapshot (historical run results) ──────────────────────────────────

class ReportSnapshot(Base):
    """Stores the result-set of every report execution (scheduled or manual).
    This enables a full history/timeline so users can compare data across dates.
    """
    __tablename__ = "report_snapshots"
    __table_args__ = (
        Index("ix_report_snapshots_report_id", "report_id"),
        Index("ix_report_snapshots_org_id", "org_id"),
        Index("ix_report_snapshots_report_created_at", "report_id", "created_at"),
        Index("ix_report_snapshots_org_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    # "manual" | "scheduled"
    triggered_by: Mapped[str] = mapped_column(String(32), default="manual", nullable=False)
    rows_data: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    rows_returned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = _ts_created()

    report: Mapped["Report"] = relationship(back_populates="snapshots")


class Dashboard(Base):
    __tablename__ = "dashboards"
    __table_args__ = (
        Index("ix_dashboards_org_id", "org_id"),
        Index("ix_dashboards_org_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    layout_config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = _ts_created()

    items: Mapped[list["DashboardReport"]] = relationship(
        back_populates="dashboard", cascade="all, delete-orphan", lazy="selectin"
    )


class DashboardReport(Base):
    __tablename__ = "dashboard_reports"
    __table_args__ = (
        UniqueConstraint("dashboard_id", "report_id", name="uq_dr_dash_report"),
        Index("ix_dashboard_reports_report_id", "report_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reports.id", ondelete="CASCADE"), nullable=False
    )
    position_x: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    position_y: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    width: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    height: Mapped[int] = mapped_column(Integer, default=4, nullable=False)
    added_at: Mapped[datetime] = _ts_created()

    dashboard: Mapped[Dashboard] = relationship(back_populates="items")
