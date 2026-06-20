from __future__ import annotations

import uuid
from datetime import datetime, timezone
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database.models import Dashboard, DashboardReport, Report
from app.core.exceptions import Conflict, Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.schemas.dashboard import (
    DashboardCreate,
    DashboardItemAdd,
    DashboardItemUpdate,
    DashboardRead,
    DashboardUpdate,
)


async def list_dashboards(
    db: AsyncIOMotorDatabase, current: CurrentUser
) -> list[DashboardRead]:
    cursor = db[Dashboard.COLLECTION].find({"org_id": str(current.org_id)})
    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [DashboardRead.model_validate(Dashboard(**d)) for d in rows]


async def get_dashboard(
    db: AsyncIOMotorDatabase, current: CurrentUser, dashboard_id: uuid.UUID
) -> Dashboard:
    d = await db[Dashboard.COLLECTION].find_one({
        "_id": str(dashboard_id),
        "org_id": str(current.org_id)
    })
    if not d:
        raise NotFound("Dashboard not found")
    return Dashboard(**d)


async def create(
    db: AsyncIOMotorDatabase, current: CurrentUser, data: DashboardCreate
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create dashboards")

    if data.is_default:
        await db[Dashboard.COLLECTION].update_many(
            {"org_id": str(current.org_id)},
            {"$set": {"is_default": False}}
        )

    d_doc = Dashboard.new(
        org_id=str(current.org_id),
        created_by=str(current.user_id),
        name=data.name,
        is_default=data.is_default,
        layout_config=data.layout_config,
    )
    d_doc["items"] = []

    await db[Dashboard.COLLECTION].insert_one(d_doc)
    return DashboardRead.model_validate(Dashboard(**d_doc))


async def update(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    data: DashboardUpdate,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update dashboards")
    d = await get_dashboard(db, current, dashboard_id)

    payload = data.model_dump(exclude_unset=True)
    if payload.get("is_default"):
        await db[Dashboard.COLLECTION].update_many(
            {"org_id": str(current.org_id)},
            {"$set": {"is_default": False}}
        )

    if payload:
        await db[Dashboard.COLLECTION].update_one(
            {"_id": str(dashboard_id)},
            {"$set": payload}
        )

    updated_doc = await db[Dashboard.COLLECTION].find_one({"_id": str(dashboard_id)})
    return DashboardRead.model_validate(Dashboard(**updated_doc))


async def delete(
    db: AsyncIOMotorDatabase, current: CurrentUser, dashboard_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete dashboards")
    d = await get_dashboard(db, current, dashboard_id)
    await db[Dashboard.COLLECTION].delete_one({"_id": str(dashboard_id)})


async def add_item(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    data: DashboardItemAdd,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)

    report = await db[Report.COLLECTION].find_one({
        "_id": str(data.report_id),
        "org_id": str(current.org_id)
    })
    if not report:
        raise NotFound("Report not found")

    existing_items = getattr(d, "items", [])
    if any(str(item.report_id) == str(data.report_id) for item in existing_items):
        raise Conflict("Report already on dashboard")

    new_item = {
        "id": str(uuid.uuid4()),
        "report_id": str(data.report_id),
        "position_x": data.position_x,
        "position_y": data.position_y,
        "width": data.width,
        "height": data.height,
        "added_at": datetime.now(timezone.utc),
    }

    await db[Dashboard.COLLECTION].update_one(
        {"_id": str(dashboard_id)},
        {"$push": {"items": new_item}}
    )

    updated_doc = await db[Dashboard.COLLECTION].find_one({"_id": str(dashboard_id)})
    return DashboardRead.model_validate(Dashboard(**updated_doc))


async def update_item(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
    data: DashboardItemUpdate,
) -> DashboardRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)

    update_fields = {}
    payload = data.model_dump(exclude_unset=True)
    for k, v in payload.items():
        update_fields[f"items.$.{k}"] = v

    if update_fields:
        res = await db[Dashboard.COLLECTION].update_one(
            {"_id": str(dashboard_id), "items.id": str(item_id)},
            {"$set": update_fields}
        )
        if res.matched_count == 0:
            raise NotFound("Dashboard item not found")

    updated_doc = await db[Dashboard.COLLECTION].find_one({"_id": str(dashboard_id)})
    return DashboardRead.model_validate(Dashboard(**updated_doc))


async def remove_item(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    dashboard_id: uuid.UUID,
    item_id: uuid.UUID,
) -> None:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot edit dashboards")
    d = await get_dashboard(db, current, dashboard_id)

    res = await db[Dashboard.COLLECTION].update_one(
        {"_id": str(dashboard_id)},
        {"$pull": {"items": {"id": str(item_id)}}}
    )
    if res.modified_count == 0:
        raise NotFound("Dashboard item not found")
