"""streaming_service — WebSocket streaming and REST fallback execution paths."""
from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.database.models import ExecutionStatus
from app.core.exceptions import LLMError, TargetDBError, ValidationFailed
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.engine import BoundQuery, SemanticResolver, extract_columns_from_sql
from app.engine.executor import execute_collect, execute_stream
from app.llm.insight_generator import generate_insight
from app.schemas.query import IntentResult, RunQueryResponse
from app.services import connection_service, session_service
from app.services.chat.helpers import determine_erp_type
from app.services.chat.history_service import record_history
from motor.motor_asyncio import AsyncIOMotorDatabase

log = get_logger(__name__)


async def run_via_rest(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    session_id: uuid.UUID,
    natural_language: str,
) -> RunQueryResponse:
    """REST fallback — translate NL → SQL → collect and return full result."""
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    try:
        org = await db["organizations"].find_one({"_id": str(current.org_id)})
    except Exception:
        org = None
    erp_type = determine_erp_type(conn, org)

    resolver = SemanticResolver(erp_type=erp_type)
    generated_sql = await resolver.translate_to_sql(natural_language)
    if generated_sql.startswith("CONVERSATIONAL:"):
        raise ValidationFailed(generated_sql[len("CONVERSATIONAL:"):])

    clean_desc = (natural_language or "Dynamic Query").strip()[:60].title()
    intent = IntentResult(
        template_id="semantic_query",
        params={},
        missing_params=[],
        confidence=1.0,
        rationale="translated_via_yaml_engine",
    )
    bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as exc:
        await record_history(
            db, session, conn, current, natural_language, intent,
            bound.sql, ExecutionStatus.error, error_message=exc.message,
        )
        raise

    history = await record_history(
        db, session, conn, current, natural_language, intent,
        bound.sql, ExecutionStatus.success,
        execution_time_ms=result.execution_time_ms,
        rows_returned=result.rows_returned,
    )

    col_names = list(result.columns) if result.columns else extract_columns_from_sql(bound.sql)

    await session_service.append_turn(db, session, role="user", content=natural_language)

    summary: str | None = None
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=result.rows, user_name=None)
        await session_service.append_turn(
            db, session, role="assistant", content=summary, columns=col_names
        )
    except LLMError as exc:
        log.warning("insight_failed", extra={"err": str(exc)})

    return RunQueryResponse(
        history_id=history.id,
        sql=bound.sql,
        rows=result.rows,
        columns=col_names,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        intent=intent,
        summary=summary,
    )


async def run_streaming(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    session_id: uuid.UUID,
    natural_language: str,
    on_event: Callable[[dict[str, Any]], Awaitable[None]],
) -> dict[str, Any]:
    """WebSocket path — streams result batches via on_event callbacks."""
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    await on_event({"type": "status", "message": "Connecting to database..."})
    await on_event({"type": "progress", "step": "intent_extraction"})

    try:
        org = await db["organizations"].find_one({"_id": str(current.org_id)})
    except Exception:
        org = None
    erp_type = determine_erp_type(conn, org)

    resolver = SemanticResolver(erp_type=erp_type)
    generated_sql = await resolver.translate_to_sql(natural_language)
    if generated_sql.startswith("CONVERSATIONAL:"):
        raise ValidationFailed(generated_sql[len("CONVERSATIONAL:"):])

    intent = IntentResult(
        template_id="semantic_query",
        params={},
        missing_params=[],
        confidence=1.0,
        rationale="translated_via_yaml_engine",
    )
    bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)

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
    except TargetDBError as exc:
        await record_history(
            db, session, conn, current, natural_language, intent,
            bound.sql, ExecutionStatus.error, error_message=exc.message,
        )
        raise

    exec_ms = int((time.perf_counter() - started) * 1000)
    history = await record_history(
        db, session, conn, current, natural_language, intent,
        bound.sql, ExecutionStatus.success,
        execution_time_ms=exec_ms,
        rows_returned=rows_returned,
    )

    await session_service.append_turn(db, session, role="user", content=natural_language)

    col_names = list(sample[0].keys()) if sample else extract_columns_from_sql(bound.sql)

    await on_event({"type": "progress", "step": "insight"})
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=sample)
        await session_service.append_turn(
            db, session, role="assistant", content=summary, columns=col_names
        )
        await on_event({"type": "insight", "summary": summary})
    except LLMError as exc:
        log.warning("insight_failed", extra={"err": str(exc)})

    return {
        "history_id": str(history.id),
        "rows_returned": rows_returned,
        "exec_time_ms": exec_ms,
        "columns": col_names,
    }
