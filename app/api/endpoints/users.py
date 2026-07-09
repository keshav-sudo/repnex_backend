from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status, HTTPException
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.api.dependencies.rate_limit import rate_limit
from app.api.dependencies.tenancy import bind_tenant_context
from app.core.database.session import get_db
from app.core.security.auth import CurrentUser
from app.core.database.models import User, PermissionRequest, PermissionRequestStatus
from app.schemas.user import (
    InviteRequest,
    InviteResponse,
    PasswordChangeRequest,
    RoleUpdateRequest,
    UserRead,
    PermissionsUpdateRequest,
    PermissionRequestCreate,
    PermissionRequestRead,
    PermissionRequestAction,
)
from app.services import invitation_service, user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def me(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> UserRead:
    return await user_service.get_me(db, current)


@router.get("", response_model=list[UserRead])
async def list_users(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> list[UserRead]:
    return await user_service.list_org_users(db, current)


@router.post("/invite", response_model=InviteResponse, status_code=status.HTTP_202_ACCEPTED)
async def invite(
    data: InviteRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("api")),
) -> InviteResponse:
    return await invitation_service.invite(
        db,
        current_user_id=current.user_id,
        current_org_id=current.org_id,
        current_role=current.role,
        data=data,
    )


@router.patch("/{user_id}/role", response_model=UserRead)
async def change_role(
    user_id: uuid.UUID,
    data: RoleUpdateRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> UserRead:
    return await user_service.update_role(db, current, user_id, data)


@router.patch("/{user_id}/permissions", response_model=UserRead)
async def update_permissions(
    user_id: uuid.UUID,
    data: PermissionsUpdateRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> UserRead:
    return await user_service.update_permissions(db, current, user_id, data)


@router.post("/me/password", status_code=status.HTTP_200_OK)
async def change_password(
    data: PasswordChangeRequest,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
    _rl: None = Depends(rate_limit("auth")),
) -> dict:
    await user_service.change_password(db, current, data.current_password, data.new_password)
    return {"ok": True}


@router.post("/request-permission", status_code=status.HTTP_201_CREATED)
async def request_permission(
    data: PermissionRequestCreate,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    # Check if a pending request already exists
    existing = await db[PermissionRequest.COLLECTION].find_one({
        "org_id": str(current.org_id),
        "user_id": str(current.user_id),
        "module_key": data.module_key,
        "status": PermissionRequestStatus.pending.value,
    })
    if existing:
        return {"ok": True, "message": "Request already pending."}

    req = PermissionRequest.new(
        org_id=str(current.org_id),
        user_id=str(current.user_id),
        module_key=data.module_key,
        status=PermissionRequestStatus.pending,
    )
    await db[PermissionRequest.COLLECTION].insert_one(req)
    return {"ok": True, "message": "Permission request submitted successfully."}


@router.get("/permission-requests", response_model=list[PermissionRequestRead])
async def list_permission_requests(
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> list[PermissionRequestRead]:
    # Admin check
    if current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage permission requests."
        )

    cursor = db[PermissionRequest.COLLECTION].aggregate([
        {
            "$match": {
                "org_id": str(current.org_id),
                "status": PermissionRequestStatus.pending.value
            }
        },
        {
            "$lookup": {
                "from": User.COLLECTION,
                "localField": "user_id",
                "foreignField": "_id",
                "as": "user_info"
            }
        },
        {
            "$unwind": {
                "path": "$user_info",
                "preserveNullAndEmptyArrays": True
            }
        }
    ])
    rows = await cursor.to_list(length=1000)

    out = []
    for r in rows:
        email = r.get("user_info", {}).get("email") or ""
        out.append(PermissionRequestRead(
            id=uuid.UUID(r["_id"]),
            org_id=uuid.UUID(r["org_id"]),
            user_id=uuid.UUID(r["user_id"]),
            module_key=r["module_key"],
            status=r["status"],
            created_at=r["created_at"],
            user_email=email
        ))
    return out


@router.post("/permission-requests/{request_id}/action")
async def act_on_permission_request(
    request_id: uuid.UUID,
    data: PermissionRequestAction,
    current: CurrentUser = Depends(bind_tenant_context),
    db: AsyncIOMotorDatabase = Depends(get_db),
) -> dict:
    # Admin check
    if current.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage permission requests."
        )

    req = await db[PermissionRequest.COLLECTION].find_one({
        "_id": str(request_id),
        "org_id": str(current.org_id)
    })
    if not req:
        raise HTTPException(status_code=404, detail="Request not found.")

    if req.get("status") != PermissionRequestStatus.pending.value:
        raise HTTPException(status_code=400, detail="Request has already been processed.")

    target_user = await db[User.COLLECTION].find_one({
        "_id": req["user_id"],
        "org_id": str(current.org_id)
    })
    if not target_user:
        raise HTTPException(status_code=404, detail="Target user not found.")

    new_status = PermissionRequestStatus.approved if data.action == "approve" else PermissionRequestStatus.denied

    perms = dict(target_user.get("module_permissions") or {})
    if data.action == "approve":
        perms[req["module_key"]] = True
        # If this is a sub-module, let's auto-enable the parent too!
        
        parent = _MODULE_KEY_TO_PARENT.get(req["module_key"])
        if parent and parent != req["module_key"]:
            perms[parent] = True
    else:
        perms[req["module_key"]] = False

    # Update request and user
    await db[PermissionRequest.COLLECTION].update_one(
        {"_id": str(request_id)},
        {"$set": {"status": new_status.value}}
    )
    await db[User.COLLECTION].update_one(
        {"_id": req["user_id"]},
        {"$set": {"module_permissions": perms}}
    )
    return {"ok": True, "message": f"Request has been {data.action}d."}
