from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from app.schemas.common import ORMBase
from pydantic import BaseModel, Field


class DashboardCreate(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255)]
    is_default: bool = False
    layout_config: dict[str, Any] = Field(default_factory=dict)


class DashboardUpdate(BaseModel):
    name: str | None = None
    is_default: bool | None = None
    layout_config: dict[str, Any] | None = None


class DashboardItemRead(ORMBase):
    id: uuid.UUID
    report_id: uuid.UUID
    position_x: int
    position_y: int
    width: int
    height: int
    added_at: datetime


class DashboardRead(ORMBase):
    id: uuid.UUID
    org_id: uuid.UUID
    created_by: uuid.UUID
    name: str
    is_default: bool
    layout_config: dict[str, Any]
    created_at: datetime
    items: list[DashboardItemRead]


class DashboardItemAdd(BaseModel):
    report_id: uuid.UUID
    position_x: int = 0
    position_y: int = 0
    width: int = Field(default=4, ge=1, le=12)
    height: int = Field(default=4, ge=1, le=12)


class DashboardItemUpdate(BaseModel):
    position_x: int | None = None
    position_y: int | None = None
    width: int | None = Field(default=None, ge=1, le=12)
    height: int | None = Field(default=None, ge=1, le=12)
