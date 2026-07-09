"""
app/core/database/models.py
───────────────────────────
MongoDB document schemas with thin class wrappers.
Allows construction via new() -> dict (for MongoDB insertion)
and instantiating via Model(**mongo_dict) (for SQLAlchemy compatibility across services).
"""
from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _uid() -> str:
    return str(uuid.uuid4())


# ── Enums ─────────────────────────────────────────────────────────────────────

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


class PermissionRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


# ── Model Wrapper Classes ─────────────────────────────────────────────────────

class Organization:
    COLLECTION = "organizations"

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID | None
    plan_type: PlanType
    hide_sql_queries: bool
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "owner_id" and v:
                self.owner_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "plan_type" and v:
                self.plan_type = PlanType(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        name: str,
        owner_id: str | None = None,
        plan_type: PlanType = PlanType.free,
        hide_sql_queries: bool = False,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "name": name,
            "owner_id": owner_id,
            "plan_type": plan_type.value,
            "hide_sql_queries": hide_sql_queries,
            "created_at": _now(),
        }


class User:
    COLLECTION = "users"

    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    hashed_password: str | None
    role: UserRole
    status: UserStatus
    invited_by: uuid.UUID | None
    module_permissions: dict[str, bool] | None
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "invited_by" and v:
                self.invited_by = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "role" and v:
                self.role = UserRole(v)
            elif k == "status" and v:
                self.status = UserStatus(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        org_id: str,
        email: str,
        hashed_password: str | None,
        role: UserRole = UserRole.viewer,
        status: UserStatus = UserStatus.pending,
        invited_by: str | None = None,
        module_permissions: dict | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "org_id": org_id,
            "email": email.lower(),
            "hashed_password": hashed_password,
            "role": role.value,
            "status": status.value,
            "invited_by": invited_by,
            "module_permissions": module_permissions,
            "created_at": _now(),
        }


class DBConnection:
    COLLECTION = "db_connections"

    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    db_type: DBType
    host: str
    port: int
    db_name: str
    encrypted_username: str
    encrypted_password: str
    ssl_enabled: bool
    is_active: bool
    last_tested_at: datetime | None
    schema_info: dict | None
    schema_last_synced_at: datetime | None
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "created_by" and v:
                self.created_by = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "db_type" and v:
                self.db_type = DBType(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        org_id: str,
        created_by: str,
        name: str,
        db_type: DBType,
        host: str,
        port: int,
        db_name: str,
        encrypted_username: str,
        encrypted_password: str,
        ssl_enabled: bool = False,
        is_active: bool = True,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "org_id": org_id,
            "created_by": created_by,
            "name": name,
            "db_type": db_type.value,
            "host": host,
            "port": port,
            "db_name": db_name,
            "encrypted_username": encrypted_username,
            "encrypted_password": encrypted_password,
            "ssl_enabled": ssl_enabled,
            "is_active": is_active,
            "last_tested_at": None,
            "schema_info": None,
            "schema_last_synced_at": None,
            "created_at": _now(),
        }


class DBConnectionAccess:
    COLLECTION = "db_connection_access"

    id: uuid.UUID
    connection_id: uuid.UUID
    user_id: uuid.UUID | None
    org_id: uuid.UUID
    granted_by: uuid.UUID
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "connection_id" and v:
                self.connection_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "user_id" and v:
                self.user_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "granted_by" and v:
                self.granted_by = uuid.UUID(v) if isinstance(v, str) else v
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        connection_id: str,
        org_id: str,
        granted_by: str,
        user_id: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "connection_id": connection_id,
            "user_id": user_id,
            "org_id": org_id,
            "granted_by": granted_by,
            "created_at": _now(),
        }


class GISession:
    COLLECTION = "gi_sessions"

    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    connection_id: uuid.UUID
    title: str
    context_window: list[dict]
    token_count: int
    status: SessionStatus
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "user_id" and v:
                self.user_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "connection_id" and v:
                self.connection_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "status" and v:
                self.status = SessionStatus(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        user_id: str,
        org_id: str,
        connection_id: str,
        title: str,
        context_window: list | None = None,
        token_count: int = 0,
        status: SessionStatus = SessionStatus.active,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "user_id": user_id,
            "org_id": org_id,
            "connection_id": connection_id,
            "title": title,
            "context_window": context_window or [],
            "token_count": token_count,
            "status": status.value,
            "created_at": _now(),
        }


class QueryHistory:
    COLLECTION = "query_history"

    id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    connection_id: uuid.UUID
    natural_language_input: str
    generated_sql: str | None
    row_size: bool | None
    intent: dict
    execution_status: ExecutionStatus
    error_message: str | None
    execution_time_ms: int | None
    rows_returned: int | None
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "session_id" and v:
                self.session_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "user_id" and v:
                self.user_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "connection_id" and v:
                self.connection_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "execution_status" and v:
                self.execution_status = ExecutionStatus(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        session_id: str,
        user_id: str,
        org_id: str,
        connection_id: str,
        natural_language_input: str,
        generated_sql: str | None,
        row_size: bool | None,
        intent: dict,
        execution_status: ExecutionStatus,
        error_message: str | None = None,
        execution_time_ms: int | None = None,
        rows_returned: int | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "session_id": session_id,
            "user_id": user_id,
            "org_id": org_id,
            "connection_id": connection_id,
            "natural_language_input": natural_language_input,
            "generated_sql": generated_sql,
            "row_size": row_size,
            "intent": intent,
            "execution_status": execution_status.value,
            "error_message": error_message,
            "execution_time_ms": execution_time_ms,
            "rows_returned": rows_returned,
            "created_at": _now(),
        }


class Report:
    COLLECTION = "reports"

    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    auto_refresh_connection_id: uuid.UUID | None
    columns: list[ReportColumn]
    name: str
    description: str | None
    query_template_id: str
    parameters: dict
    is_public: bool
    is_pinned: bool
    refresh_interval_days: int | None
    next_refresh_at: datetime | None
    last_refreshed_at: datetime | None
    created_at: datetime

    def __init__(self, **kwargs):
        self.columns = []
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "created_by" and v:
                self.created_by = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "auto_refresh_connection_id" and v:
                self.auto_refresh_connection_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "columns" and isinstance(v, list):
                self.columns = [ReportColumn(**col) if isinstance(col, dict) else col for col in v]
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        org_id: str,
        created_by: str,
        name: str,
        description: str | None,
        query_template_id: str,
        parameters: dict,
        is_public: bool = False,
        is_pinned: bool = False,
        refresh_interval_days: int | None = None,
        next_refresh_at: datetime | None = None,
        last_refreshed_at: datetime | None = None,
        auto_refresh_connection_id: str | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "org_id": org_id,
            "created_by": created_by,
            "name": name,
            "description": description,
            "query_template_id": query_template_id,
            "parameters": parameters,
            "is_public": is_public,
            "is_pinned": is_pinned,
            "refresh_interval_days": refresh_interval_days,
            "next_refresh_at": next_refresh_at,
            "last_refreshed_at": last_refreshed_at,
            "auto_refresh_connection_id": auto_refresh_connection_id,
            "created_at": _now(),
        }


class ReportColumn:
    COLLECTION = "report_columns"

    id: uuid.UUID
    report_id: uuid.UUID
    column_name: str
    display_name: str
    position: int
    is_visible: bool
    data_type: str
    format_config: dict

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "report_id" and v:
                self.report_id = uuid.UUID(v) if isinstance(v, str) else v
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        report_id: str,
        column_name: str,
        display_name: str,
        position: int,
        is_visible: bool = True,
        data_type: str,
        format_config: dict | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "report_id": report_id,
            "column_name": column_name,
            "display_name": display_name,
            "position": position,
            "is_visible": is_visible,
            "data_type": data_type,
            "format_config": format_config or {},
        }


class ReportSnapshot:
    COLLECTION = "report_snapshots"

    id: uuid.UUID
    report_id: uuid.UUID
    org_id: uuid.UUID
    triggered_by: str
    rows_data: list[dict]
    rows_returned: int
    execution_time_ms: int | None
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "report_id" and v:
                self.report_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        report_id: str,
        org_id: str,
        triggered_by: str = "manual",
        rows_data: list | None = None,
        rows_returned: int = 0,
        execution_time_ms: int | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "report_id": report_id,
            "org_id": org_id,
            "triggered_by": triggered_by,
            "rows_data": rows_data or [],
            "rows_returned": rows_returned,
            "execution_time_ms": execution_time_ms,
            "created_at": _now(),
        }


class Dashboard:
    COLLECTION = "dashboards"

    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    is_default: bool
    layout_config: dict
    created_at: datetime

    def __init__(self, **kwargs):
        self.items = []
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "created_by" and v:
                self.created_by = uuid.UUID(v) if isinstance(v, str) else v
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        org_id: str,
        created_by: str,
        name: str,
        is_default: bool = False,
        layout_config: dict | None = None,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "org_id": org_id,
            "created_by": created_by,
            "name": name,
            "is_default": is_default,
            "layout_config": layout_config or {},
            "created_at": _now(),
        }


class DashboardReport:
    COLLECTION = "dashboard_reports"

    id: uuid.UUID
    dashboard_id: uuid.UUID
    report_id: uuid.UUID
    position_x: int
    position_y: int
    width: int
    height: int
    added_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "dashboard_id" and v:
                self.dashboard_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "report_id" and v:
                self.report_id = uuid.UUID(v) if isinstance(v, str) else v
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        dashboard_id: str,
        report_id: str,
        position_x: int = 0,
        position_y: int = 0,
        width: int = 4,
        height: int = 4,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "dashboard_id": dashboard_id,
            "report_id": report_id,
            "position_x": position_x,
            "position_y": position_y,
            "width": width,
            "height": height,
            "added_at": _now(),
        }


class PermissionRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"


class PermissionRequest:
    COLLECTION = "permission_requests"

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    module_key: str
    status: PermissionRequestStatus
    created_at: datetime

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            if k == "_id":
                self.id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "org_id" and v:
                self.org_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "user_id" and v:
                self.user_id = uuid.UUID(v) if isinstance(v, str) else v
            elif k == "status" and v:
                self.status = PermissionRequestStatus(v)
            else:
                setattr(self, k, v)

    @staticmethod
    def new(
        *,
        org_id: str,
        user_id: str,
        module_key: str,
        status: PermissionRequestStatus = PermissionRequestStatus.pending,
        doc_id: str | None = None,
    ) -> dict:
        return {
            "_id": doc_id or _uid(),
            "org_id": org_id,
            "user_id": user_id,
            "module_key": module_key,
            "status": status.value,
            "created_at": _now(),
        }
