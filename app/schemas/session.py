from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from app.schemas.common import ORMBase

SessionStatus = Literal["active", "archived"]


class SessionCreate(BaseModel):
    connection_id: uuid.UUID
    title: Annotated[str | None, Field(max_length=255)] = None


class SessionUpdate(BaseModel):
    title: str | None = None
    status: SessionStatus | None = None


class SessionRead(ORMBase):
    id: uuid.UUID
    user_id: uuid.UUID
    org_id: uuid.UUID
    connection_id: uuid.UUID
    title: str
    token_count: int
    status: SessionStatus
    created_at: datetime


class SessionDetail(SessionRead):
    context_window: list[dict]
