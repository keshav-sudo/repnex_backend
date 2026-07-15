"""execute_service — handles explicit param submission after params_needed response.

Retrieves the original NL query from session history, re-translates with the
user-supplied date range, then executes against the target database.
"""
from __future__ import annotations

from app.core.database.models import ExecutionStatus
from app.core.exceptions import TargetDBError, ValidationFailed
from app.core.logging import get_logger
from app.core.security.auth import CurrentUser
from app.engine import BoundQuery, SemanticResolver, execute_collect, extract_columns_from_sql
from app.llm.insight_generator import generate_insight
from app.llm.suggestion_generator import generate_suggestions
from app.schemas.query import ChatResponse, ExecuteRequest, IntentResult
from app.services import connection_service, session_service
from app.services.chat.helpers import detect_module_from_query, determine_erp_type
from app.services.chat.history_service import record_history
from motor.motor_asyncio import AsyncIOMotorDatabase

log = get_logger(__name__)

_DEFAULT_SUGGESTIONS = ["Show AP invoice list", "Top 10 customers"]


async def execute_with_params(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    data: ExecuteRequest,
) -> ChatResponse:
    """Execute a semantic query with user-provided date parameters.

    Requires a ``session_id`` so the original NL query can be retrieved from
    the session's context window (the most recent user turn that is not an
    "Execute report" command).
    """
    # ── Load session ──────────────────────────────────────────────────────
    session = None
    if data.session_id:
        session = await session_service.get(db, current, data.session_id)
        if session.org_id != current.org_id:
            raise ValidationFailed("Session does not belong to your organisation.")

    if not session or not session.context_window:
        raise ValidationFailed("Could not retrieve original query context from session.")

    # Find the original NL query from session turns
    nl: str | None = None
    for turn in reversed(session.context_window):
        if turn.get("role") == "user":
            content = turn.get("content", "")
            if not content.startswith(("Execute report", "Execute template")):
                nl = content
                break

    if not nl:
        if session.title and session.title not in ("New chat", ""):
            nl = session.title
        else:
            raise ValidationFailed("Could not retrieve original query from session history.")

    from app.services.chat.helpers import check_module_access
    module = detect_module_from_query(nl)
    is_allowed, deny_msg = check_module_access(module, current)
    if not is_allowed:
        return ChatResponse(
            type="access_denied",
            message=deny_msg,
            suggestions=_DEFAULT_SUGGESTIONS,
        )

    start_date = data.params.get("start_date")
    end_date = data.params.get("end_date")

    # ── ERP resolution ─────────────────────────────────────────────────────
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

    # ── Translate with date params ─────────────────────────────────────────
    target_dialect = conn.db_type.value if conn else None
    resolver = SemanticResolver(erp_type=erp_type, target_dialect=target_dialect)
    try:
        generated_sql = await resolver.translate_to_sql(nl, start_date=start_date, end_date=end_date)
    except Exception as exc:
        log.error("execute_translation_failed", extra={"err": str(exc)}, exc_info=True)
        return ChatResponse(
            type="error",
            message=f"Semantic translation failed: {exc}",
            suggestions=_DEFAULT_SUGGESTIONS,
        )

    if generated_sql.startswith("CONVERSATIONAL:"):
        msg = generated_sql[len("CONVERSATIONAL:"):]
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=msg, type="conversational",
                    suggestions=_DEFAULT_SUGGESTIONS,
                )
            except Exception as exc:
                log.warning("session_append_failed", extra={"err": str(exc)})
        return ChatResponse(type="conversational", message=msg, suggestions=_DEFAULT_SUGGESTIONS)

    # ── Build metadata ─────────────────────────────────────────────────────
    clean_desc = nl.strip()[:60].title() if nl else "Dynamic Query"
    intent = IntentResult(
        template_id="semantic_query",
        params=data.params,
        missing_params=[],
        confidence=1.0,
        rationale="translated_via_yaml_engine",
    )

    nl_repr = f"Execute report '{clean_desc}' with parameters: {data.params}"
    if session:
        try:
            await session_service.append_turn(db, session, role="user", content=nl_repr)
        except Exception as exc:
            log.warning("session_append_failed", extra={"err": str(exc)})

    # ── Execute ────────────────────────────────────────────────────────────
    conn = await connection_service.get_connection(db, current, data.connection_id)
    bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as exc:
        exc.message = f"{exc.message}\n\nExecuted SQL:\n{bound.sql}"
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=f"Database error: {exc.message}"
                )
                await record_history(
                    db, session, conn, current, nl, intent, bound.sql,
                    ExecutionStatus.error, error_message=exc.message,
                )
            except Exception as inner:
                log.warning("record_history_failed", extra={"err": str(inner)})
        return ChatResponse(
            type="error",
            message=f"Database error: {exc.message}",
            sql=bound.sql,
            template_id="semantic_query",
        )

    # ── Insight + suggestions ──────────────────────────────────────────────
    user_name = current.email.split("@")[0] if "@" in current.email else current.email
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

    msg = summary or f"Query executed. {result.rows_returned} rows returned."
    col_names = list(result.columns) if result.columns else extract_columns_from_sql(bound.sql)

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
            await record_history(
                db, session, conn, current, nl, intent, bound.sql,
                ExecutionStatus.success,
                execution_time_ms=result.execution_time_ms,
                rows_returned=result.rows_returned,
            )
        except Exception as exc:
            log.warning("record_history_success_failed", extra={"err": str(exc)})

    return ChatResponse(
        type="executable",
        message=msg,
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
