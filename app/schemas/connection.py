from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Literal
from urllib.parse import urlparse, unquote

from pydantic import BaseModel, Field, model_validator

from app.schemas.common import ORMBase

DBType = Literal["postgres", "mysql", "mssql", "oracle", "cloudsql"]


def _parse_connection_string(cs: str) -> dict:
    """
    Parse both ADO.NET (SQL Server / SysPro) and SQLAlchemy URL connection strings.

    Supported formats:
      ADO.NET:
        Server=host,port;Database=db;User Id=user;Password=pass;
        Data Source=host\\instance;Initial Catalog=db;User ID=user;Password=pass;
      SQLAlchemy URL:
        mssql+pyodbc://user:pass@host:port/db
        postgresql://user:pass@host:port/db
        mysql+pymysql://user:pass@host/db
    """
    result: dict = {}

    # ── SQLAlchemy / standard URL ───────────────────────────────────────
    if "://" in cs:
        parsed = urlparse(cs)
        scheme = parsed.scheme.split("+")[0].lower()  # mssql, postgresql, mysql …
        mapping = {
            "mssql": "mssql",
            "sqlserver": "mssql",
            "postgresql": "postgres",
            "postgres": "postgres",
            "mysql": "mysql",
            "oracle": "oracle",
        }
        result["db_type"] = mapping.get(scheme, "mssql")
        result["host"] = parsed.hostname or ""
        result["port"] = parsed.port or {"mssql": 1433, "postgres": 5432, "mysql": 3306, "oracle": 1521}.get(result["db_type"], 1433)
        result["db_name"] = parsed.path.lstrip("/")
        result["username"] = unquote(parsed.username or "")
        result["password"] = unquote(parsed.password or "")
        return result

    # ── ADO.NET key=value style ─────────────────────────────────────────
    pairs: dict[str, str] = {}
    for part in cs.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        pairs[k.strip().lower()] = v.strip()

    # Server / Data Source can encode host,port or host\instance
    server_raw = pairs.get("server") or pairs.get("data source") or ""
    if server_raw:
        # Strip tcp: prefix used by some drivers
        server_raw = re.sub(r"^tcp:", "", server_raw, flags=re.IGNORECASE).strip()
        if "," in server_raw:                 # host,port
            host_part, port_part = server_raw.split(",", 1)
            result["host"] = host_part.strip()
            result["port"] = int(port_part.strip())
        elif "\\" in server_raw:              # host\instance — drop instance name
            result["host"] = server_raw.split("\\")[0].strip()
            result["port"] = 1433
        else:
            result["host"] = server_raw
            result["port"] = 1433

    result["db_name"] = (
        pairs.get("database")
        or pairs.get("initial catalog")
        or pairs.get("db")
        or ""
    )
    result["username"] = (
        pairs.get("user id")
        or pairs.get("uid")
        or pairs.get("user")
        or pairs.get("username")
        or ""
    )
    result["password"] = pairs.get("password") or pairs.get("pwd") or ""

    # Detect SSL
    encrypt_val = pairs.get("encrypt", "false").lower()
    result["ssl_enabled"] = encrypt_val in ("true", "yes", "1")

    # Default to MSSQL for ADO.NET strings
    result["db_type"] = "mssql"
    return result


class ConnectionCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    db_type: DBType | None = None                               # inferred from connection_string if omitted
    host: Annotated[str, Field(min_length=0, max_length=255)] = ""
    port: int = 0
    db_name: Annotated[str, Field(min_length=0, max_length=255)] = ""
    username: Annotated[str, Field(min_length=0, max_length=255)] = ""
    password: Annotated[str, Field(min_length=0, max_length=512)] = ""
    ssl_enabled: bool = False
    connection_string: str | None = None                        # optional raw connection string

    @model_validator(mode="after")
    def _resolve_from_connection_string(self) -> "ConnectionCreate":
        if self.connection_string:
            parsed = _parse_connection_string(self.connection_string)
            if not self.db_type:
                self.db_type = parsed.get("db_type", "mssql")  # type: ignore[assignment]
            if not self.host:
                self.host = parsed.get("host", "")
            if not self.port:
                self.port = parsed.get("port", 1433)
            if not self.db_name:
                self.db_name = parsed.get("db_name", "")
            if not self.username:
                self.username = parsed.get("username", "")
            if not self.password:
                self.password = parsed.get("password", "")
            if not self.ssl_enabled:
                self.ssl_enabled = parsed.get("ssl_enabled", False)

        # Final validation after resolution
        if not self.db_type:
            raise ValueError("db_type is required when connection_string is not provided")
        if self.host.startswith("gateway:") or self.host == "gateway":
            agent_part = self.host.split("gateway:")[1].strip() if "gateway:" in self.host else ""
            if not agent_part or agent_part == "":
                raise ValueError("Agent Name is required in Secure Gateway mode")
            # For gateway, traditional port and credentials validation is bypassed
            pass
        else:
            if not self.host:
                raise ValueError("host is required (or provide a connection_string)")
            if not self.port:
                default_ports = {
                    "postgres": 5432,
                    "mysql": 3306,
                    "mssql": 1433,
                    "oracle": 1521,
                    "cloudsql": 5432,
                }
                self.port = default_ports.get(self.db_type, 1433)
                if not self.port:
                    raise ValueError("port is required (or provide a connection_string)")
            if not self.db_name:
                raise ValueError("db_name is required (or provide a connection_string)")
        return self


class ListDatabasesRequest(BaseModel):
    """Credentials needed to connect to a server and list its databases.
    db_name is intentionally optional — we connect to the default/master DB
    just to enumerate what exists.
    """
    db_type: DBType
    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: int = 0
    username: Annotated[str, Field(min_length=1, max_length=255)]
    password: Annotated[str, Field(min_length=0, max_length=512)] = ""
    ssl_enabled: bool = False

    @model_validator(mode="after")
    def _resolve_defaults(self) -> "ListDatabasesRequest":
        if not self.port:
            default_ports = {
                "postgres": 5432,
                "mysql": 3306,
                "mssql": 1433,
                "oracle": 1521,
                "cloudsql": 5432,
            }
            self.port = default_ports.get(self.db_type, 1433)
        return self


class ConnectionUpdate(BaseModel):
    name: str | None = None
    host: str | None = None
    port: int | None = None
    db_name: str | None = None
    username: str | None = None
    password: str | None = None
    ssl_enabled: bool | None = None
    is_active: bool | None = None


class ConnectionRead(ORMBase):
    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    db_type: DBType
    host: str
    port: int
    db_name: str
    ssl_enabled: bool
    is_active: bool
    last_tested_at: datetime | None
    created_at: datetime


class TestConnectionResponse(BaseModel):
    ok: bool
    latency_ms: int | None = None
    error: str | None = None


class ListDatabasesResponse(BaseModel):
    databases: list[str]


class AccessGrantRequest(BaseModel):
    user_id: uuid.UUID | None = None  # None = whole org


class AccessGrantRead(ORMBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    user_id: uuid.UUID | None
    granted_by: uuid.UUID
    created_at: datetime
