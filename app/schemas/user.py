from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, EmailStr, Field

from app.schemas.common import ORMBase

Role = Literal["admin", "editor", "viewer"]


class UserRead(ORMBase):
    id: uuid.UUID
    org_id: uuid.UUID
    email: EmailStr
    role: Role
    status: str
    invited_by: uuid.UUID | None = None
    module_permissions: dict[str, bool] | None = None
    created_at: datetime


class InviteRequest(BaseModel):
    email: EmailStr
    role: Role = "viewer"


class InviteResponse(BaseModel):
    user_id: uuid.UUID
    status: str


class RoleUpdateRequest(BaseModel):
    role: Role


class PermissionsUpdateRequest(BaseModel):
    module_permissions: dict[str, bool]


class UserUpdate(BaseModel):
    email: EmailStr | None = None


class PasswordChangeRequest(BaseModel):
    current_password: Annotated[str, Field(min_length=1, max_length=128)]
    new_password: Annotated[str, Field(min_length=8, max_length=128)]
