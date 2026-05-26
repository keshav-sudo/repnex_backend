from __future__ import annotations

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database.session import get_db
from app.schemas.auth import (
    AcceptInviteRequest,
    AuthResponse,
    LoginRequest,
    RefreshRequest,
    SignupRequest,
    TokenPair,
)
from app.services import auth_service, invitation_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def signup(data: SignupRequest, db: AsyncSession = Depends(get_db)) -> AuthResponse:
    return await auth_service.signup(db, data)


@router.post("/login", response_model=AuthResponse)
async def login(data: LoginRequest, db: AsyncSession = Depends(get_db)) -> AuthResponse:
    return await auth_service.login(db, data)


@router.post("/refresh", response_model=TokenPair)
async def refresh(data: RefreshRequest, db: AsyncSession = Depends(get_db)) -> TokenPair:
    return await auth_service.refresh(db, data.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
async def logout(data: RefreshRequest) -> None:
    await auth_service.logout(data.refresh_token)


@router.post("/accept-invite", response_model=AuthResponse)
async def accept_invite(
    data: AcceptInviteRequest, db: AsyncSession = Depends(get_db)
) -> AuthResponse:
    return await invitation_service.accept(db, data)
