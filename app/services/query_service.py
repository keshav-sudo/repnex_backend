"""Query service — orchestrates intent classification, RAG retrieval, and execution."""
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
from app.core.pinecone_client import get_pinecone_store_optional
from app.core.security.auth import CurrentUser
from app.llm.insight_generator import generate_insight
from app.llm.intent_extractor import (
    classify_intent,
    extract_intent,
    generate_conversational_response,
)
from app.llm.suggestion_generator import generate_suggestions
from app.query_engine.executor import execute_collect, execute_stream
from app.query_engine.parameter_binder import bind, find_missing_params
from app.query_engine.template_loader import (
    create_template_from_pinecone,
    get_template_registry,
)
from app.schemas.query import (
    ChatRequest,
    ChatResponse,
    ExecuteRequest,
    IntentResult,
    MissingParam,
    RunQueryResponse,
    TemplateMatch,
)
from app.services import connection_service, session_service

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# NEW: Unified chat endpoint
# ═══════════════════════════════════════════════════════════════════════

async def chat(
    db: AsyncSession,
    current: CurrentUser,
    *,
    data: ChatRequest,
) -> ChatResponse:
    """
    Main entry point for the new intent-engine flow.
    1. Classify (conversational vs executable)
    2a. If conversational → LLM response
    2b. If executable → Pinecone search → intent extract → bind → execute
    """
    s = get_settings()
    nl = data.natural_language

    # ── Step 1: Classify intent ──────────────────────────────────────
    try:
        classification = await classify_intent(nl)
    except LLMError as e:
        log.warning("classify_failed", extra={"err": str(e)})
        # Default to executable on LLM failure
        classification = None

    is_conversational = (
        classification
        and classification.type == "conversational"
        and classification.confidence >= 0.7
    )

    # ── Step 2a: Conversational response ─────────────────────────────
    if is_conversational:
        try:
            message = await generate_conversational_response(nl)
        except LLMError:
            message = "I'm sorry, I couldn't process that. Try asking a data question like 'Show overdue invoices'."

        return ChatResponse(
            type="conversational",
            message=message,
            suggestions=[
                "Show AP ageing report",
                "List overdue supplier invoices",
                "Top 10 customers by revenue",
                "Stock on hand summary",
            ],
        )

    # ── Step 2b: Executable flow ─────────────────────────────────────
    store = get_pinecone_store_optional()
    registry = get_template_registry()

    # Search Pinecone for matching templates
    template_candidates: list[dict[str, Any]] = []
    if store:
        try:
            template_candidates = store.search_templates(nl, top_k=5)
        except Exception as e:
            log.warning("pinecone_search_failed", extra={"err": str(e)})

    # Fall back to static registry if no Pinecone results
    if not template_candidates:
        template_candidates = registry.list_for_llm()[:10]

    # ── Step 3: Extract intent from candidates ───────────────────────
    try:
        intent = await extract_intent(
            nl,
            template_candidates=template_candidates,
        )
    except LLMError as e:
        return ChatResponse(
            type="error",
            message=f"Could not understand your query: {e.message}",
            suggestions=["Show AP ageing report", "List overdue invoices"],
        )

    if not intent.template_id or intent.confidence < s.INTENT_MIN_CONFIDENCE:
        return ChatResponse(
            type="error",
            message="I couldn't match your query to a specific report template. Try rephrasing.",
            suggestions=[
                "Show AP ageing report",
                "List overdue supplier invoices",
                "Top customers by revenue",
            ],
            candidates=[
                TemplateMatch(
                    id=c["id"],
                    score=c.get("score", 0),
                    description=c.get("description", ""),
                    module=c.get("module", ""),
                    category=c.get("category", ""),
                )
                for c in template_candidates[:5]
            ],
        )

    # ── Step 4: Get the template ─────────────────────────────────────
    template_meta = None
    for c in template_candidates:
        if c["id"] == intent.template_id:
            template_meta = c
            break

    if template_meta:
        template = create_template_from_pinecone(template_meta)
    elif registry.has(intent.template_id):
        template = registry.get(intent.template_id)
        template_meta = {
            "module": template.module,
            "category": template.category,
            "description": template.description,
        }
    else:
        return ChatResponse(
            type="error",
            message=f"Template '{intent.template_id}' not found.",
        )

    # ── Step 5: Check for missing params ─────────────────────────────
    missing = find_missing_params(template, intent.params)

    if missing:
        # Generate suggestions in parallel
        try:
            suggestions = await generate_suggestions(
                template_id=template.id,
                module=template.module,
                category=template.category,
                description=template.description,
            )
        except Exception:
            suggestions = []

        top_suggestions = [
            TemplateMatch(
                id=c["id"],
                score=c.get("score", 0),
                description=c.get("description", ""),
                module=c.get("module", ""),
                category=c.get("category", ""),
            )
            for c in template_candidates[:5]
        ]

        return ChatResponse(
            type="params_needed",
            message=f"I found the right query: **{template.description}**. Please provide the missing parameters:",
            template_id=template.id,
            template_description=template.description,
            template_module=template.module,
            extracted_params=intent.params,
            missing_params=missing,
            intent=intent,
            suggestions=suggestions,
            candidates=top_suggestions,
        )

    # ── Step 6: Execute the query (or preview if no DB) ─────────────────
    if not data.connection_id:
        # No DB connected — return template preview + suggestions so the user
        # still gets useful info about what the query WOULD do.
        top_suggestions = [
            TemplateMatch(
                id=c["id"],
                score=c.get("score", 0),
                description=c.get("description", ""),
                module=c.get("module", ""),
                category=c.get("category", ""),
            )
            for c in template_candidates[:5]
        ]

        try:
            suggestions = await generate_suggestions(
                template_id=template.id,
                module=template.module,
                category=template.category,
                description=template.description,
            )
        except Exception:
            suggestions = [
                "Show AP ageing report",
                "List overdue supplier invoices",
                "Top customers by revenue",
                "Stock on hand summary",
            ]

        # Show the SQL preview (pick first available dialect)
        sql_preview: str | None = None
        if template.sql_by_dialect:
            sql_preview = next(iter(template.sql_by_dialect.values()))

        return ChatResponse(
            type="template_preview",
            message=(
                f"✅ I matched your query to: **{template.description}**\n\n"
                f"📂 Module: {template.module} → {template.category}\n\n"
                f"To run this report, please **connect a database** from the Connections page. "
                f"Here's a preview of the SQL that will execute:"
            ),
            template_id=template.id,
            template_description=template.description,
            template_module=template.module,
            extracted_params=intent.params,
            missing_params=[],
            sql=sql_preview,
            candidates=top_suggestions,
            suggestions=suggestions,
            intent=intent,
        )

    conn = await connection_service.get_connection(db, current, data.connection_id)
    db_type = conn.db_type.value

    try:
        bound = bind(template, intent.params, db_type=db_type)
    except ValidationFailed as e:
        return ChatResponse(
            type="error",
            message=f"Parameter validation failed: {e.message}",
            template_id=template.id,
            extracted_params=intent.params,
        )

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as e:
        return ChatResponse(
            type="error",
            message=f"Database error: {e.message}",
            sql=bound.sql,
            template_id=template.id,
        )

    # Generate insight summary
    summary: str | None = None
    try:
        summary = await generate_insight(
            intent=intent.model_dump(), rows=result.rows
        )
    except LLMError as e:
        log.warning("insight_failed", extra={"err": str(e)})

    # Generate follow-up suggestions
    try:
        suggestions = await generate_suggestions(
            template_id=template.id,
            module=template.module,
            category=template.category,
            description=template.description,
        )
    except Exception:
        suggestions = []

    top_suggestions = [
        TemplateMatch(
            id=c["id"],
            score=c.get("score", 0),
            description=c.get("description", ""),
            module=c.get("module", ""),
            category=c.get("category", ""),
        )
        for c in template_candidates[:5]
    ]

    return ChatResponse(
        type="executable",
        message=summary or f"Query executed successfully. Found {result.rows_returned} rows.",
        template_id=template.id,
        template_description=template.description,
        template_module=template.module,
        extracted_params=intent.params,
        sql=bound.sql,
        rows=result.rows,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        summary=summary,
        suggestions=suggestions,
        intent=intent,
        candidates=top_suggestions,
    )


# ═══════════════════════════════════════════════════════════════════════
# NEW: Direct execute with explicit params
# ═══════════════════════════════════════════════════════════════════════

async def execute_with_params(
    db: AsyncSession,
    current: CurrentUser,
    *,
    data: ExecuteRequest,
) -> ChatResponse:
    """Execute a template with user-provided params (after params_needed)."""
    registry = get_template_registry()
    store = get_pinecone_store_optional()

    # Try Pinecone first for the template
    template = None
    template_meta = None
    if store:
        try:
            results = store.search_templates(data.template_id, top_k=1)
            for r in results:
                if r["id"] == data.template_id:
                    template = create_template_from_pinecone(r)
                    template_meta = r
                    break
        except Exception:
            pass

    if template is None and registry.has(data.template_id):
        template = registry.get(data.template_id)

    if template is None:
        return ChatResponse(type="error", message=f"Template '{data.template_id}' not found.")

    conn = await connection_service.get_connection(db, current, data.connection_id)
    db_type = conn.db_type.value

    try:
        bound = bind(template, data.params, db_type=db_type)
    except ValidationFailed as e:
        return ChatResponse(
            type="error",
            message=f"Parameter error: {e.message}",
            template_id=template.id,
        )

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as e:
        return ChatResponse(type="error", message=f"Database error: {e.message}", sql=bound.sql)

    summary: str | None = None
    try:
        intent_dict = {"template_id": template.id, "params": data.params}
        summary = await generate_insight(intent=intent_dict, rows=result.rows)
    except LLMError:
        pass

    try:
        suggestions = await generate_suggestions(
            template_id=template.id,
            module=template.module,
            category=template.category,
            description=template.description,
        )
    except Exception:
        suggestions = []

    return ChatResponse(
        type="executable",
        message=summary or f"Executed. {result.rows_returned} rows returned.",
        template_id=template.id,
        template_description=template.description,
        template_module=template.module,
        extracted_params=data.params,
        sql=bound.sql,
        rows=result.rows,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        summary=summary,
        suggestions=suggestions,
    )


# ═══════════════════════════════════════════════════════════════════════
# LEGACY: Original REST endpoint (kept for backward compat)
# ═══════════════════════════════════════════════════════════════════════

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
    except LLMError as e:
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


# ── private helpers ──────────────────────────────────────────────────

async def _intent(natural_language: str, ctx: list) -> IntentResult:
    s = get_settings()

    # Try Pinecone first
    store = get_pinecone_store_optional()
    if store:
        try:
            candidates = store.search_templates(natural_language, top_k=5)
            if candidates:
                intent = await extract_intent(
                    natural_language,
                    template_candidates=candidates,
                    context_window=ctx,
                )
                if intent.template_id and intent.confidence >= s.INTENT_MIN_CONFIDENCE:
                    return intent
        except Exception as e:
            log.warning("pinecone_intent_failed", extra={"err": str(e)})

    # Fall back to static catalog
    catalog = get_template_registry().list_for_llm()
    intent = await extract_intent(
        natural_language, template_candidates=catalog, context_window=ctx
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
