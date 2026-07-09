from __future__ import annotations

from typing import Any, TypeVar

from app.schemas.common import PageMeta, PaginatedResponse, PaginationParams

T = TypeVar("T")


async def paginate(
    collection,
    query_filter: dict[str, Any],
    params: PaginationParams,
    *,
    sort_fields: list[tuple[str, int]] | None = None,
    transform=lambda x: x,
) -> PaginatedResponse:
    total = await collection.count_documents(query_filter)

    offset = (params.page - 1) * params.page_size
    cursor = collection.find(query_filter)
    if sort_fields:
        cursor = cursor.sort(sort_fields)
    cursor = cursor.skip(offset).limit(params.page_size)
    rows = await cursor.to_list(length=params.page_size)

    return PaginatedResponse(
        items=[transform(r) for r in rows],
        meta=PageMeta(page=params.page, page_size=params.page_size, total=total),
    )
