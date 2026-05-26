from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase

DBType = Literal["postgres", "mysql", "mssql", "oracle", "cloudsql"]


class ConnectionCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    db_type: DBType
    host: Annotated[str, Field(min_length=1, max_length=255)]
    port: Annotated[int, Field(ge=1, le=65535)]
    db_name: Annotated[str, Field(min_length=1, max_length=255)]
    username: Annotated[str, Field(min_length=1, max_length=255)]
    password: Annotated[str, Field(min_length=1, max_length=512)]
    ssl_enabled: bool = False


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


class AccessGrantRequest(BaseModel):
    user_id: uuid.UUID | None = None  # None = whole org


class AccessGrantRead(ORMBase):
    id: uuid.UUID
    connection_id: uuid.UUID
    user_id: uuid.UUID | None
    granted_by: uuid.UUID
    created_at: datetime
