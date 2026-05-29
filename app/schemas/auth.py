from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, EmailStr, Field, AliasChoices

from app.schemas.common import ORMBase


class SignupRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=8, max_length=128)]
    org_name: Annotated[
        str,
        Field(
            default="",
            max_length=255,
            validation_alias=AliasChoices("org_name", "company"),
        ),
    ] = ""


class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class RefreshRequest(BaseModel):
    refresh_token: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: Annotated[str, Field(min_length=8, max_length=128)]


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserPublic(ORMBase):
    id: uuid.UUID
    org_id: uuid.UUID
    email: EmailStr
    role: str
    status: str
    name: str = ""
    company: str = ""
    organizationId: uuid.UUID | None = None
    organizationName: str = ""
    onboardingCompleted: bool = True


class OrgPublic(ORMBase):
    id: uuid.UUID
    name: str
    plan_type: str


class AuthResponse(BaseModel):
    tokens: TokenPair
    user: UserPublic
    org: OrgPublic
    token: str = ""
