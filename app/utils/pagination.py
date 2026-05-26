from __future__ import annotations

from typing import TypeVar

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.common import PageMeta, PaginatedResponse, PaginationParams

T = TypeVar("T")


async def paginate(
    db: AsyncSession,
    base_query: Select,
    params: PaginationParams,
    *,
    transform=lambda x: x,
) -> PaginatedResponse:
    total_q = select(func.count()).select_from(base_query.subquery())
    total = (await db.execute(total_q)).scalar_one()

    offset = (params.page - 1) * params.page_size
    rows = (
        (await db.execute(base_query.offset(offset).limit(params.page_size)))
        .scalars()
        .all()
    )
    return PaginatedResponse(
        items=[transform(r) for r in rows],
        meta=PageMeta(page=params.page, page_size=params.page_size, total=total),
    )
