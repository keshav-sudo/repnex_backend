from datetime import datetime, timezone
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
    doc_id = str(uuid.uuid4())
    history_doc = {
        "_id": doc_id,
        "session_id": str(session.id),
        "user_id": str(current.user_id),
        "org_id": str(current.org_id),
        "connection_id": str(conn.id),
        "natural_language_input": natural_language,
        "generated_sql": sql,
        "intent": intent.model_dump() if hasattr(intent, "model_dump") else intent,
        "execution_status": status.value if hasattr(status, "value") else str(status),
        "error_message": error_message,
        "execution_time_ms": execution_time_ms,
        "rows_returned": rows_returned,
        "created_at": datetime.now(timezone.utc),
    }

    try:
        # pyrefly: ignore [missing-attribute]
        await db[QueryHistory.COLLECTION].insert_one(history_doc)
    except Exception as exc:  # noqa: BLE001
        log.warning("history_insert_failed", extra={"err": str(exc)})

    return QueryHistory(**history_doc)
