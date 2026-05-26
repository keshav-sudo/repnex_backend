from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database.models import (
    DBConnection,
    ExecutionStatus,
    GISession,
    QueryHistory,
)
from app.core.exceptions import (
    AppError,
    LLMError,
    NotFound,
    TargetDBError,
    ValidationFailed,
)
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.llm.insight_generator import generate_insight
from app.llm.intent_extractor import extract_intent
from app.query_engine.executor import execute_collect, execute_stream
from app.query_engine.parameter_binder import bind
from app.query_engine.template_loader import get_template_registry
from app.schemas.query import IntentResult, RunQueryResponse
from app.services import connection_service, session_service

log = get_logger(__name__)


async def run_via_rest(
    db: AsyncSession,
    current: CurrentUser,
    *,
    session_id: uuid.UUID,
    natural_language: str,
) -> RunQueryResponse:
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    intent = await _intent(natural_language, session.context_window)
    template = get_template_registry().get(intent.template_id)
    bound = bind(template, intent.params, db_type=conn.db_type.value)

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as e:
        await _record_history(
            db, session, conn, current, natural_language, intent, bound.sql,
            ExecutionStatus.error, error_message=e.message
        )
        raise

    history = await _record_history(
        db, session, conn, current, natural_language, intent, bound.sql,
        ExecutionStatus.success,
        execution_time_ms=result.execution_time_ms,
        rows_returned=result.rows_returned,
    )

    await session_service.append_turn(db, session, role="user", content=natural_language)

    summary: str | None = None
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=result.rows)
        await session_service.append_turn(db, session, role="assistant", content=summary)
    except LLMError as e:  # insight failure is non-fatal
        log.warning("insight_failed", extra={"err": str(e)})

    return RunQueryResponse(
        history_id=history.id,
        sql=bound.sql,
        rows=result.rows,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        intent=intent,
        summary=summary,
    )


async def run_streaming(
    db: AsyncSession,
    current: CurrentUser,
    *,
    session_id: uuid.UUID,
    natural_language: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, Any]:
    """Used by WebSocket. Emits events via `on_event`."""
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    await on_event({"type": "status", "message": "Connecting to database..."})
    await on_event({"type": "progress", "step": "intent_extraction"})

    intent = await _intent(natural_language, session.context_window)
    template = get_template_registry().get(intent.template_id)
    bound = bind(template, intent.params, db_type=conn.db_type.value)

    await on_event({"type": "progress", "step": "sql_build"})
    await on_event({"type": "sql", "sql": bound.sql})
    await on_event({"type": "progress", "step": "execute"})

    started = time.perf_counter()
    rows_returned = 0
    sample: list[dict[str, Any]] = []
    batch_no = 0
    try:
        async for batch in execute_stream(conn, bound):
            batch_no += 1
            rows_returned += len(batch)
            if len(sample) < 50:
                sample.extend(batch[: 50 - len(sample)])
            await on_event({"type": "data", "batch": batch_no, "rows": batch})
    except TargetDBError as e:
        await _record_history(
            db, session, conn, current, natural_language, intent, bound.sql,
            ExecutionStatus.error, error_message=e.message
        )
        raise

    exec_ms = int((time.perf_counter() - started) * 1000)
    history = await _record_history(
        db, session, conn, current, natural_language, intent, bound.sql,
        ExecutionStatus.success,
        execution_time_ms=exec_ms,
        rows_returned=rows_returned,
    )
    await session_service.append_turn(db, session, role="user", content=natural_language)

    await on_event({"type": "progress", "step": "insight"})
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=sample)
        await session_service.append_turn(db, session, role="assistant", content=summary)
        await on_event({"type": "insight", "summary": summary})
    except LLMError as e:
        log.warning("insight_failed", extra={"err": str(e)})

    return {
        "history_id": str(history.id),
        "rows_returned": rows_returned,
        "exec_time_ms": exec_ms,
    }


async def _intent(natural_language: str, ctx: list) -> IntentResult:
    s = get_settings()
    catalog = get_template_registry().list_for_llm()
    intent = await extract_intent(
        natural_language, templates_catalog=catalog, context_window=ctx
    )
    if not intent.template_id or intent.confidence < s.INTENT_MIN_CONFIDENCE:
        raise ValidationFailed(
            "Could not match a query template with sufficient confidence",
            suggestions=[t["id"] for t in catalog],
            confidence=intent.confidence,
        )
    return intent


async def _record_history(
    db: AsyncSession,
    session: GISession,
    conn: DBConnection,
    current: CurrentUser,
    nl: str,
    intent: IntentResult,
    sql: str | None,
    status: ExecutionStatus,
    *,
    execution_time_ms: int | None = None,
    rows_returned: int | None = None,
    error_message: str | None = None,
) -> QueryHistory:
    h = QueryHistory(
        session_id=session.id,
        user_id=current.user_id,
        connection_id=conn.id,
        natural_language_input=nl,
        generated_sql=sql,
        intent=intent.model_dump(),
        execution_status=status,
        error_message=error_message,
        execution_time_ms=execution_time_ms,
        rows_returned=rows_returned,
    )
    db.add(h)
    await db.commit()
    await db.refresh(h)
    return h
