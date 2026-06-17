from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from app.schemas.common import ORMBase

PlanType = Literal["free", "pro", "enterprise"]


class OrgRead(ORMBase):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID | None
    plan_type: PlanType
    hide_sql_queries: bool
    created_at: datetime


class OrgUpdate(BaseModel):
    name: str | None = None
    plan_type: PlanType | None = None
    hide_sql_queries: bool | None = None

