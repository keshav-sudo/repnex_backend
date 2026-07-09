"""chat_service — main V2 semantic chat entry point.

Pipeline:
  1. Classify intent (conversational → respond, executable → continue).
  2a. Conversational → personalised LLM response.
  2b. Executable → SemanticResolver → SQL → execute → insight.
"""
from __future__ import annotations

from app.core.database.models import ExecutionStatus
from app.core.exceptions import LLMError, TargetDBError
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.engine import BoundQuery, SemanticResolver, execute_collect, extract_columns_from_sql
from app.llm.insight_generator import generate_insight

# pyrefly: ignore [missing-import]
from app.llm.intent_classifier import classify_intent, generate_conversational_response
from app.llm.suggestion_generator import generate_suggestions
from app.schemas.query import (
    ChatRequest,
    ChatResponse,
    IntentResult,
    MissingParam,
)
from app.services import connection_service, session_service
from app.services.chat.helpers import (
    detect_date_dependency,
    detect_module_from_query,
    determine_erp_type,
    resolve_relative_date_range,
)
from app.services.chat.history_service import record_history
from motor.motor_asyncio import AsyncIOMotorDatabase

log = get_logger(__name__)

_DEFAULT_SUGGESTIONS = ["Show AP invoice list", "Top 10 customers"]
_CONVERSATIONAL_SUGGESTIONS = [
    "Show AP ageing report",
    "List overdue supplier invoices",
    "Top 10 customers by revenue",
    "Stock on hand summary",
]


async def chat(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    data: ChatRequest,
) -> ChatResponse:
    """Semantic chat — classify intent, translate NL → SQL, execute, return insight."""
    nl = data.natural_language

    # Personalisation
    user_name: str | None = None
    ai_tone: str = "friendly"
    if data.personalization:
        user_name = data.personalization.preferred_name or data.personalization.display_name or None
        ai_tone = data.personalization.ai_tone or "friendly"
    if not user_name:
        user_name = current.email.split("@")[0] if "@" in current.email else current.email

    # Session tracking
    session = None
    if data.session_id:
        try:
            session = await session_service.get(db, current, data.session_id)
            await session_service.append_turn(db, session, role="user", content=nl)
        except Exception as exc:
            log.warning("session_load_failed", extra={"err": str(exc)})

    # ── 1. Classify intent ──────────────────────────────────────────────────
    classification = None
    try:
        classification = await classify_intent(nl, user_name=user_name)
    except LLMError as exc:
        log.warning("classify_failed", extra={"err": str(exc)})

    is_conversational = (
        classification
        and classification.type == "conversational"
        and classification.confidence >= 0.7
    )

    # ── 2a. Conversational path ─────────────────────────────────────────────
    if is_conversational:
        try:
            message = await generate_conversational_response(nl, user_name=user_name, ai_tone=ai_tone)
        except LLMError:
            message = "I couldn't process that. Try asking a data question like 'Show overdue invoices'."

        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=message,
                    type="conversational", suggestions=_CONVERSATIONAL_SUGGESTIONS,
                )
            except Exception as exc:
                log.warning("session_append_failed", extra={"err": str(exc)})

        return ChatResponse(
            type="conversational",
            message=message,
            suggestions=_CONVERSATIONAL_SUGGESTIONS,
        )

    # ── 2b. Executable — resolve ERP + connection ───────────────────────────
    conn = None
    if data.connection_id:
        try:
            conn = await connection_service.get_connection(db, current, data.connection_id)
        except Exception:
            pass

    try:
        org = await db["organizations"].find_one({"_id": str(current.org_id)})
    except Exception:
        org = None

    erp_type = determine_erp_type(conn, org)
    start_date, end_date = resolve_relative_date_range(nl)

    # ── Translate NL → SQL via SemanticResolver ─────────────────────────────
    resolver = SemanticResolver(erp_type=erp_type)
    try:
        history_window = list(session.context_window[:-1]) if session and len(session.context_window) > 1 else None
        generated_sql = await resolver.translate_to_sql(
            nl, start_date=start_date, end_date=end_date, history=history_window
        )
    except Exception as exc:
        log.error("semantic_translation_failed", extra={"err": str(exc)}, exc_info=True)
        return ChatResponse(
            type="error",
            message=f"Semantic translation failed: {exc}",
            suggestions=_DEFAULT_SUGGESTIONS,
        )

    # LLM returned a conversational clarification (out-of-schema)
    if generated_sql.startswith("CONVERSATIONAL:"):
        msg = generated_sql[len("CONVERSATIONAL:"):]
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=msg,
                    type="conversational",
                    suggestions=["Show AP ageing report", "Top customers by revenue"],
                )
            except Exception as exc:
                log.warning("session_append_failed", extra={"err": str(exc)})
        return ChatResponse(
            type="conversational",
            message=msg,
            suggestions=["Show AP ageing report", "Top customers by revenue"],
        )

    # ── Date gate — prompt user if date is needed but not provided ──────────
    if not start_date and detect_date_dependency(generated_sql, nl):
        clean_desc = _make_description(nl)
        msg = "I found the right query. Please provide a date range to execute it:"
        intent = IntentResult(
            template_id="semantic_query",
            params={},
            missing_params=["start_date", "end_date"],
            confidence=1.0,
            rationale="date_dependency_detected",
        )
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=msg, type="params_needed",
                    template_id="semantic_query", template_description=clean_desc,
                    extracted_params={}, suggestions=_DEFAULT_SUGGESTIONS,
                )
            except Exception as exc:
                log.warning("session_append_failed", extra={"err": str(exc)})
        return ChatResponse(
            type="params_needed",
            message=msg,
            template_id="semantic_query",
            template_description=clean_desc,
            template_module="semantic_engine",
            extracted_params={},
            missing_params=[
                MissingParam(name="start_date", type="date", description="Start Date", required=True),
                MissingParam(name="end_date", type="date", description="End Date", required=True),
            ],
            suggestions=_DEFAULT_SUGGESTIONS,
            candidates=[],
            intent=intent,
        )

    # ── Build intent metadata ────────────────────────────────────────────────
    clean_desc = _make_description(nl)
    intent = IntentResult(
        template_id="semantic_query",
        params={"start_date": start_date, "end_date": end_date} if start_date else {},
        missing_params=[],
        confidence=1.0,
        rationale="translated_via_yaml_engine",
    )

    # ── Preview — no DB connected ────────────────────────────────────────────
    if not data.connection_id:
        msg = (
            f"✅ Query translated via Semantic Engine ({erp_type.upper()})\n\n"
            "To run this report, **connect a database** from the Connections page. "
            "Here's a preview of the SQL:"
        )
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=msg, type="template_preview",
                    template_id="semantic_query", template_description=clean_desc,
                    extracted_params=intent.params, sql=generated_sql,
                    suggestions=_DEFAULT_SUGGESTIONS,
                )
            except Exception as exc:
                log.warning("session_append_failed", extra={"err": str(exc)})
        return ChatResponse(
            type="template_preview",
            message=msg,
            template_id="semantic_query",
            template_description=clean_desc,
            template_module="semantic_engine",
            extracted_params=intent.params,
            missing_params=[],
            sql=generated_sql,
            candidates=[],
            suggestions=_DEFAULT_SUGGESTIONS,
            intent=intent,
        )

    # ── Execute ──────────────────────────────────────────────────────────────
    conn = await connection_service.get_connection(db, current, data.connection_id)
    bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as exc:
        history_id = None
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=f"Database error: {exc.message}"
                )
                hist = await record_history(
                    db, session, conn, current, nl, intent, bound.sql,
                    ExecutionStatus.error, error_message=exc.message,
                )
                history_id = str(hist.id)
            except Exception as inner:
                log.warning("record_history_failed", extra={"err": str(inner)})
        return ChatResponse(
            type="error",
            message=f"Database error: {exc.message}",
            history_id=history_id,
            sql=bound.sql,
            template_id="semantic_query",
        )

    # ── Insight + suggestions ────────────────────────────────────────────────
    summary: str | None = None
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=result.rows, user_name=user_name)
    except Exception as exc:
        log.warning("insight_failed", extra={"err": str(exc)})

    suggestions: list[str] = []
    try:
        module = detect_module_from_query(nl) or "ap"
        suggestions = await generate_suggestions(
            template_id="semantic_query", module=module, category="query",
            description=clean_desc, user_name=user_name,
        )
    except Exception as exc:
        log.warning("suggestions_failed", extra={"err": str(exc)})
    if not suggestions:
        suggestions = _DEFAULT_SUGGESTIONS

    msg = summary or f"Query executed successfully. {result.rows_returned} rows returned."
    col_names = list(result.columns) if result.columns else extract_columns_from_sql(bound.sql)

    history_id = None
    if session:
        try:
            await session_service.append_turn(
                db, session, role="assistant", content=msg, type="executable",
                sql=bound.sql, rows=result.rows, columns=col_names,
                rows_returned=result.rows_returned,
                execution_time_ms=result.execution_time_ms,
                template_id="semantic_query", template_description=clean_desc,
                extracted_params=intent.params, suggestions=suggestions,
            )
            hist = await record_history(
                db, session, conn, current, nl, intent, bound.sql,
                ExecutionStatus.success,
                execution_time_ms=result.execution_time_ms,
                rows_returned=result.rows_returned,
            )
            history_id = str(hist.id)
        except Exception as exc:
            log.warning("record_history_success_failed", extra={"err": str(exc)})

    return ChatResponse(
        type="executable",
        message=msg,
        history_id=history_id,
        template_id="semantic_query",
        template_description=clean_desc,
        template_module="semantic_engine",
        extracted_params=intent.params,
        sql=bound.sql,
        rows=result.rows,
        columns=col_names,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        summary=summary,
        suggestions=suggestions,
        intent=intent,
        candidates=[],
    )


def _make_description(nl: str) -> str:
    desc = (nl or "Dynamic Query").strip()
    if len(desc) > 60:
        desc = desc[:57] + "..."
    return desc.title()
