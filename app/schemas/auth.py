from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import AliasChoices, BaseModel, EmailStr, Field, field_validator

from app.schemas.common import ORMBase


def _validate_password_complexity(password: str) -> str:
    if not any(ch.isupper() for ch in password):
        raise ValueError("Password must include at least one uppercase letter")
    if not any(ch.islower() for ch in password):
        raise ValueError("Password must include at least one lowercase letter")
    if not any(ch.isdigit() for ch in password):
        raise ValueError("Password must include at least one number")
    return password


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
    otp: str

    @field_validator("password")
    @classmethod
    def password_complexity(cls, value: str) -> str:
        return _validate_password_complexity(value)


class SendOtpRequest(BaseModel):
    email: EmailStr



class LoginRequest(BaseModel):
    email: EmailStr
    password: Annotated[str, Field(min_length=1, max_length=128)]


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    password: Annotated[str, Field(min_length=8, max_length=128)]

    @field_validator("password")
    @classmethod
    def password_complexity(cls, value: str) -> str:
        return _validate_password_complexity(value)


class RefreshRequest(BaseModel):
    refresh_token: str


class AcceptInviteRequest(BaseModel):
    token: str
    password: Annotated[str, Field(min_length=8, max_length=128)]

    @field_validator("password")
    @classmethod
    def password_complexity(cls, value: str) -> str:
        return _validate_password_complexity(value)


class InvitePreview(BaseModel):
    email: EmailStr
    organization_name: str
    role: str
    status: str


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
    module_permissions: dict[str, bool] | None = None


class OrgPublic(ORMBase):
    id: uuid.UUID
    name: str
    plan_type: str


class AuthResponse(BaseModel):
    tokens: TokenPair
    user: UserPublic
    org: OrgPublic
    token: str = ""
