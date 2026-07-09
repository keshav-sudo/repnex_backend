"""history_service — records chat query history to MongoDB."""
from __future__ import annotations

import uuid
from typing import Any

from app.core.database.models import (
    DBConnection,
    ExecutionStatus,
    QueryHistory,
)
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.schemas.query import IntentResult
from motor.motor_asyncio import AsyncIOMotorDatabase

log = get_logger(__name__)


async def record_history(
    db: AsyncIOMotorDatabase,
    session: Any,
    conn: DBConnection,
    current: CurrentUser,
    natural_language: str,
    intent: IntentResult,
    sql: str,
    status: ExecutionStatus,
    *,
    error_message: str | None = None,
    execution_time_ms: int | None = None,
    rows_returned: int | None = None,
) -> QueryHistory:
    """Persist a query execution record and return the saved document."""
    history = QueryHistory(
        id=uuid.uuid4(),
        org_id=current.org_id,
        user_id=current.user_id,
        session_id=session.id,
        connection_id=conn.id,
        natural_language=natural_language,
        template_id=intent.template_id,
        extracted_params=intent.params,
        generated_sql=sql,
        status=status,
        error_message=error_message,
        execution_time_ms=execution_time_ms,
        rows_returned=rows_returned,
    )
    try:
        # pyrefly: ignore [missing-attribute]
        await db[QueryHistory.COLLECTION].insert_one(history.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001
        log.warning("history_insert_failed", extra={"err": str(exc)})
    return history
