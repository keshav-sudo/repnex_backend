"""Query service — orchestrates intent classification, RAG retrieval, and execution."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import get_settings
from app.core.database.models import (
    DBConnection,
    ExecutionStatus,
    GISession,
    QueryHistory,
)
from app.core.exceptions import (
    Forbidden,
    LLMError,
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
    TemplateRegistry,
    create_template_from_pinecone,
    get_template_registry,
)
from app.schemas.query import (
    ChatRequest,
    ChatResponse,
    ExecuteRequest,
    IntentResult,
    RunQueryResponse,
    TemplateMatch,
)
from app.services import connection_service, session_service

log = get_logger(__name__)


def _detect_date_dependency(sql: str, nl: str) -> bool:
    sql_lower = sql.lower()
    nl_lower = nl.lower()

    # Temporal phrases in natural language query
    nl_indicators = [
        "last ", "ytd", "year to date", "this year", "this month", "this quarter",
        "date range", "between", "month to date", "mtd", "quarter to date", "qtd"
    ]
    for ind in nl_indicators:
        if ind in nl_lower:
            return True

    # SQL indicators that strongly suggest date range filtering
    sql_indicators = [
        "dateadd", "datefromparts"
    ]
    for ind in sql_indicators:
        if ind in sql_lower:
            return True

    # Explicit comparison operators on date columns in SQL
    date_cols = ["invoicedate", "duedate", "journaldate", "chequedate", "paymentdate", "lastpurchdate"]
    for col in date_cols:
        if col in sql_lower:
            import re
            if re.search(rf"\b{col}\s*(>=|<=|>|<|between)\b", sql_lower):
                return True

    return False



# ═══════════════════════════════════════════════════════════════════════
# NEW: Unified chat endpoint
# ═══════════════════════════════════════════════════════════════════════


async def chat(
    db: AsyncIOMotorDatabase,
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

    # Build user context for personalization
    user_name: str | None = None
    ai_tone: str = "friendly"
    if data.personalization:
        user_name = data.personalization.preferred_name or data.personalization.display_name or None
        ai_tone = data.personalization.ai_tone or "friendly"
    if not user_name:
        user_name = current.email.split("@")[0] if "@" in current.email else current.email

    session = None
    if data.session_id:
        try:
            session = await session_service.get(db, current, data.session_id)
            await session_service.append_turn(db, session, role="user", content=nl)
        except Exception as e:
            log.warning("session_load_or_append_failed", extra={"err": str(e)})

    # ── Step 1: Classify intent ──────────────────────────────────────
    try:
        classification = await classify_intent(nl, user_name=user_name)
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
            message = await generate_conversational_response(
                nl, user_name=user_name, ai_tone=ai_tone
            )
        except LLMError:
            message = "I'm sorry, I couldn't process that. Try asking a data question like 'Show overdue invoices'."

        conversational_suggestions = [
            "Show AP ageing report",
            "List overdue supplier invoices",
            "Top 10 customers by revenue",
            "Stock on hand summary",
        ]
        if session:
            try:
                await session_service.append_turn(
                    db, session, role="assistant", content=message,
                    type="conversational", suggestions=conversational_suggestions,
                )
            except Exception as e:
                log.warning("session_append_assistant_conversational_failed", extra={"err": str(e)})

        return ChatResponse(
            type="conversational",
            message=message,
            suggestions=conversational_suggestions,
        )

    # ── Step 2b: Executable flow ─────────────────────────────────────
    if s.ENGINE_VERSION.lower().strip() == "v2":
        # Determine the ERP type dynamically (syspro or epicor)
        erp_type = "syspro"
        if data.connection_id:
            try:
                conn = await connection_service.get_connection(db, current, data.connection_id)
                if conn and conn.name and "epicor" in conn.name.lower():
                    erp_type = "epicor"
            except Exception:
                pass

        try:
            org = await db["organizations"].find_one({"_id": str(current.org_id)})
            if org and org.get("erpSystem"):
                system_name = org.get("erpSystem", "").lower()
                if "epicor" in system_name:
                    erp_type = "epicor"
                elif "syspro" in system_name:
                    erp_type = "syspro"
        except Exception:
            pass

        # Initialize Semantic Resolver for the target ERP
        from app.query_engine.semantic_resolver import SemanticResolver
        resolver = SemanticResolver(erp_type=erp_type)

        try:
            generated_sql = await resolver.translate_to_sql(nl)
        except Exception as e:
            log.error(f"V2 semantic translation failed: {e}", exc_info=True)
            return ChatResponse(
                type="error",
                message=f"V2 semantic translation failed: {str(e)}",
                suggestions=["Show AP invoice list", "Top 10 customers"],
            )

        # Check if the query requires a date range
        if _detect_date_dependency(generated_sql, nl):
            from app.schemas.query import MissingParam

            missing_params = [
                MissingParam(
                    name="start_date",
                    type="date",
                    description="Start Date",
                    required=True
                ),
                MissingParam(
                    name="end_date",
                    type="date",
                    description="End Date",
                    required=True
                )
            ]

            msg = "I found the right query. Please provide the missing date range parameters to execute it correctly:"
            suggestions = ["Show AP invoice list", "Top 10 customers"]

            intent = IntentResult(
                template_id="v2_semantic_query",
                params={},
                missing_params=["start_date", "end_date"],
                confidence=1.0,
                rationale="date_dependency_detected"
            )

            if session:
                try:
                    await session_service.append_turn(
                        db,
                        session,
                        role="assistant",
                        content=msg,
                        type="params_needed",
                        template_id="v2_semantic_query",
                        template_description="Dynamic V2 Semantic Query",
                        extracted_params={},
                        suggestions=suggestions,
                    )
                except Exception as e:
                    log.warning("session_append_assistant_params_needed_failed", extra={"err": str(e)})

            return ChatResponse(
                type="params_needed",
                message=msg,
                template_id="v2_semantic_query",
                template_description="Dynamic V2 Semantic Query",
                template_module="semantic_engine",
                extracted_params={},
                missing_params=missing_params,
                suggestions=suggestions,
                candidates=[],
                intent=intent,
            )

        # Construct the mock/dynamic SQLTemplate & IntentResult to feed the remaining pipeline
        from app.query_engine.template_loader import SQLTemplate

        template = SQLTemplate(
            id="v2_semantic_query",
            description="Dynamic V2 Semantic Query",
            module="semantic_engine",
            category="query",
            supported_dbs=("mssql", "postgres", "mysql", "oracle", "cloudsql"),
            params={},
            derived_params=(),
            sql_by_dialect={erp_type: generated_sql},
            result_columns=(),
            keywords=(),
            embedding_text=""
        )

        intent = IntentResult(
            template_id="v2_semantic_query",
            params={},
            missing_params=[],
            confidence=1.0,
            rationale="translated_via_yaml_engine"
        )

        # ── Execute or Preview ──────────────────────────────────────────
        if not data.connection_id:
            sql_preview = generated_sql
            msg = (
                f"✅ I translated your query via V2 Semantic Engine ({erp_type.upper()})\n\n"
                f"To run this report, please **connect a database** from the Connections page. "
                f"Here's a preview of the SQL that will execute:"
            )
            suggestions = ["Show AP invoice list", "Top 10 customers"]
            if session:
                try:
                    await session_service.append_turn(
                        db,
                        session,
                        role="assistant",
                        content=msg,
                        type="template_preview",
                        template_id=template.id,
                        template_description=template.description,
                        extracted_params=intent.params,
                        sql=sql_preview,
                        suggestions=suggestions,
                    )
                except Exception as e:
                    log.warning("session_append_assistant_preview_failed", extra={"err": str(e)})

            return ChatResponse(
                type="template_preview",
                message=msg,
                template_id=template.id,
                template_description=template.description,
                template_module=template.module,
                extracted_params=intent.params,
                missing_params=[],
                sql=sql_preview,
                candidates=[],
                suggestions=suggestions,
                intent=intent,
            )

        # Execute the generated SQL on the connection
        conn = await connection_service.get_connection(db, current, data.connection_id)
        db_type = conn.db_type.value

        from app.query_engine.parameter_binder import BoundQuery
        bound = BoundQuery(sql=generated_sql, params={}, db_type=db_type)

        try:
            result = await execute_collect(conn, bound)
        except TargetDBError as e:
            if session:
                try:
                    await session_service.append_turn(
                        db, session, role="assistant", content=f"Database error: {e.message}"
                    )
                    await _record_history(
                        db,
                        session,
                        conn,
                        current,
                        nl,
                        intent,
                        bound.sql,
                        ExecutionStatus.error,
                        error_message=e.message,
                    )
                except Exception as ex:
                    log.warning("record_history_failed_error", extra={"err": str(ex)})
            return ChatResponse(
                type="error",
                message=f"Database error: {e.message}",
                sql=bound.sql,
                template_id=template.id,
            )

        summary: str | None = None
        suggestions = ["Show AP invoice list", "Top 10 customers"]
        try:
            summary = await generate_insight(
                intent=intent.model_dump(), rows=result.rows, user_name=user_name
            )
        except Exception as e:
            log.warning("insight_failed", extra={"err": str(e)})

        msg = summary or f"Query executed successfully. Found {result.rows_returned} rows."
        col_names = list(result.columns) if result.columns else []

        if session:
            try:
                await session_service.append_turn(
                    db,
                    session,
                    role="assistant",
                    content=msg,
                    type="executable",
                    sql=bound.sql,
                    rows=result.rows,
                    columns=col_names,
                    rows_returned=result.rows_returned,
                    execution_time_ms=result.execution_time_ms,
                    template_id=template.id,
                    template_description=template.description,
                    extracted_params=intent.params,
                    suggestions=suggestions,
                )
                await _record_history(
                    db,
                    session,
                    conn,
                    current,
                    nl,
                    intent,
                    bound.sql,
                    ExecutionStatus.success,
                    execution_time_ms=result.execution_time_ms,
                    rows_returned=result.rows_returned,
                )
            except Exception as e:
                log.warning("record_history_success_failed", extra={"err": str(e)})

        return ChatResponse(
            type="executable",
            message=msg,
            template_id=template.id,
            template_description=template.description,
            template_module=template.module,
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
    else:
        # Legacy V1 template-matching flow
        store = get_pinecone_store_optional()
        registry = get_template_registry()

        # Detect module hint from natural language for Pinecone filtering
        module_filter = _detect_module_from_query(nl)

        # ── Backend RBAC: Check module access before any LLM/Pinecone work ──
        is_allowed, deny_msg = _check_module_access(module_filter, current)
        if not is_allowed:
            log.warning(
                "module_access_denied",
                extra={
                    "user_id": str(current.user_id),
                    "role": current.role,
                    "detected_module": module_filter,
                },
            )
            return ChatResponse(
                type="access_denied",
                message=deny_msg,
                template_module=module_filter,
                suggestions=[
                    "Try a Finance or Inventory report instead",
                    "Contact your admin to request module access",
                ],
            )

        # Search Pinecone for matching templates (with keyword reranking)
        template_candidates: list[dict[str, Any]] = []
        if store:
            try:
                template_candidates = store.search_with_rerank(
                    nl, top_k=5, module_filter=module_filter
                )
            except Exception as e:
                log.warning("pinecone_search_failed", extra={"err": str(e)})
                # Fallback to basic search without module filter
                try:
                    template_candidates = store.search_templates(nl, top_k=5)
                except Exception:
                    pass

        # Fall back to keyword-matched static registry if no Pinecone results
        if not template_candidates:
            template_candidates = _smart_static_fallback(registry, nl)

        # ── Step 3: Intent extraction via LLM ────────────────────────────
        top = template_candidates[0] if template_candidates else None
        top_score = top.get("score", 0) if top else 0

        # Tier 1: Very high confidence (>= 0.60) — skip LLM entirely, use Pinecone directly
        if top_score >= 0.60:
            intent_obj = IntentResult(
                template_id=top["id"],
                params={},
                missing_params=[],
                confidence=top_score,
                rationale="direct_pinecone_match",
            )
            log.info(
                "intent_direct_pinecone_match",
                extra={"template_id": top["id"], "score": top_score, "query": nl},
            )
            intent = intent_obj
        else:
            # Tier 2: Run LLM for moderate or low scores
            try:
                intent = await extract_intent(
                    nl,
                    template_candidates=template_candidates,
                    user_name=user_name,
                )
            except LLMError as e:
                return ChatResponse(
                    type="error",
                    message=f"Could not understand your query: {e.message}",
                    suggestions=["Show AP ageing report", "List overdue invoices"],
                )

            # If LLM didn't pick a template but Pinecone has moderate confidence, auto-promote
            if not intent.template_id and top and top_score >= 0.40:
                intent.template_id = top["id"]
                intent.confidence = top_score
                intent.params = intent.params or {}
                log.info(
                    "intent_auto_promoted",
                    extra={"template_id": intent.template_id, "score": top_score, "query": nl},
                )
            elif not intent.template_id:
                # Truly no match — return helpful suggestions from candidates
                candidate_suggestions = [
                    c["description"] for c in template_candidates[:5] if c.get("description")
                ]
                return ChatResponse(
                    type="error",
                    message=(
                        "I couldn't find a specific report for that query. "
                        "Try one of these related reports below, or rephrase your question."
                    ),
                    suggestions=candidate_suggestions or [
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

        # ── Step 4: Get template ─────────────────────────────────────────
        template_meta = None
        template = None
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
            # Fetch directly from Pinecone if not in candidates or registry (e.g. from history/context)
            if store:
                try:
                    meta = store.get_template_by_id(intent.template_id)
                    if meta:
                        template = create_template_from_pinecone(meta)
                        template_meta = meta
                except Exception as e:
                    log.warning("pinecone_fetch_template_failed_in_chat", extra={"template_id": intent.template_id, "err": str(e)})

            if not template or not template_meta:
                return ChatResponse(
                    type="error",
                    message=f"Template '{intent.template_id}' not found.",
                )

        # ── Step 5: Check for missing params ─────────────────────────────
        missing = find_missing_params(template, intent.params)

        if missing:
            try:
                suggestions = await generate_suggestions(
                    template_id=template.id,
                    module=template.module,
                    category=template.category,
                    description=template.description,
                    user_name=user_name,
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

        # ── Step 6: Execute query (or preview if no DB) ─────────────────
        if not data.connection_id:
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
                    user_name=user_name,
                )
            except Exception:
                suggestions = [
                    "Show AP ageing report",
                    "List overdue supplier invoices",
                    "Top customers by revenue",
                    "Stock on hand summary",
                ]

            sql_preview: str | None = None
            if template.sql_by_dialect:
                sql_preview = next(iter(template.sql_by_dialect.values()))

            msg = (
                f"✅ I matched your query to: **{template.description}**\n\n"
                f"📂 Module: {template.module} → {template.category}\n\n"
                f"To run this report, please **connect a database** from the Connections page. "
                f"Here's a preview of the SQL that will execute:"
            )

            if session:
                try:
                    await session_service.append_turn(
                        db,
                        session,
                        role="assistant",
                        content=msg,
                        type="template_preview",
                        template_id=template.id,
                        template_description=template.description,
                        extracted_params=intent.params,
                        sql=sql_preview,
                        suggestions=suggestions,
                    )
                except Exception as e:
                    log.warning("session_append_assistant_preview_failed", extra={"err": str(e)})

            return ChatResponse(
                type="template_preview",
                message=msg,
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
            if session:
                try:
                    await session_service.append_turn(
                        db, session, role="assistant", content=f"Database error: {e.message}"
                    )
                    await _record_history(
                        db,
                        session,
                        conn,
                        current,
                        nl,
                        intent,
                        bound.sql,
                        ExecutionStatus.error,
                        error_message=e.message,
                    )
                except Exception as ex:
                    log.warning("record_history_failed_error", extra={"err": str(ex)})
            return ChatResponse(
                type="error",
                message=f"Database error: {e.message}",
                sql=bound.sql,
                template_id=template.id,
            )

        summary = None
        suggestions = []
        try:
            insight_task = generate_insight(
                intent=intent.model_dump(), rows=result.rows, user_name=user_name
            )
            suggestions_task = generate_suggestions(
                template_id=template.id,
                module=template.module,
                category=template.category,
                description=template.description,
                user_name=user_name,
            )
            results_parallel = await asyncio.gather(
                insight_task, suggestions_task, return_exceptions=True
            )
            if not isinstance(results_parallel[0], Exception):
                summary = results_parallel[0]
            else:
                log.warning("insight_failed", extra={"err": str(results_parallel[0])})
            if not isinstance(results_parallel[1], Exception):
                suggestions = results_parallel[1]
        except Exception as e:
            log.warning("parallel_llm_failed", extra={"err": str(e)})

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

        msg = summary or f"Query executed successfully. Found {result.rows_returned} rows."
        col_names = []
        if result.columns:
            col_names = list(result.columns)
        elif template and template.result_columns:
            col_names = list(template.result_columns)

        if not col_names and bound and bound.sql:
            from app.query_engine.semantic_resolver import extract_columns_from_sql
            col_names = extract_columns_from_sql(bound.sql)

        if session:
            try:
                await session_service.append_turn(
                    db,
                    session,
                    role="assistant",
                    content=msg,
                    type="executable",
                    sql=bound.sql,
                    rows=result.rows,
                    columns=col_names,
                    rows_returned=result.rows_returned,
                    execution_time_ms=result.execution_time_ms,
                    template_id=template.id,
                    template_description=template.description,
                    extracted_params=intent.params,
                    suggestions=suggestions,
                )
                await _record_history(
                    db,
                    session,
                    conn,
                    current,
                    nl,
                    intent,
                    bound.sql,
                    ExecutionStatus.success,
                    execution_time_ms=result.execution_time_ms,
                    rows_returned=result.rows_returned,
                )
            except Exception as e:
                log.warning("record_history_success_failed", extra={"err": str(e)})

        return ChatResponse(
            type="executable",
            message=msg,
            template_id=template.id,
            template_description=template.description,
            template_module=template.module,
            extracted_params=intent.params,
            sql=bound.sql,
            rows=result.rows,
            columns=col_names,
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
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    data: ExecuteRequest,
) -> ChatResponse:
    """Execute a template with user-provided params (after params_needed)."""
    s = get_settings()
    if s.ENGINE_VERSION.lower().strip() == "v2" or data.template_id == "v2_semantic_query":
        # Load session if provided
        session = None
        if data.session_id:
            session = await session_service.get(db, current, data.session_id)
            if session.org_id != current.org_id:
                raise Forbidden("Session does not belong to your organization")

        if not session or not session.context_window:
            raise ValidationFailed("Could not retrieve original query context from session.")

        # Find the original query by searching backwards for a user turn that isn't a parameter execution log
        nl = None
        for turn in reversed(session.context_window):
            if turn.get("role") == "user":
                content = turn.get("content", "")
                if not (content.startswith("Execute report") or content.startswith("Execute template")):
                    nl = content
                    break

        if not nl:
            raise ValidationFailed("Could not retrieve original query from session history.")

        # Extract dates
        start_date = data.params.get("start_date")
        end_date = data.params.get("end_date")

        # Determine ERP type
        erp_type = "syspro"
        if data.connection_id:
            try:
                conn = await connection_service.get_connection(db, current, data.connection_id)
                if conn and conn.name and "epicor" in conn.name.lower():
                    erp_type = "epicor"
            except Exception:
                pass

        try:
            org = await db["organizations"].find_one({"_id": str(current.org_id)})
            if org and org.get("erpSystem"):
                system_name = org.get("erpSystem", "").lower()
                if "epicor" in system_name:
                    erp_type = "epicor"
                elif "syspro" in system_name:
                    erp_type = "syspro"
        except Exception:
            pass

        # Initialize Semantic Resolver for the target ERP
        from app.query_engine.semantic_resolver import SemanticResolver
        resolver = SemanticResolver(erp_type=erp_type)

        try:
            generated_sql = await resolver.translate_to_sql(nl, start_date=start_date, end_date=end_date)
        except Exception as e:
            log.error(f"V2 semantic translation failed: {e}", exc_info=True)
            return ChatResponse(
                type="error",
                message=f"V2 semantic translation failed: {str(e)}",
                suggestions=["Show AP invoice list", "Top 10 customers"],
            )

        # Construct the mock/dynamic SQLTemplate & IntentResult to feed the remaining pipeline
        from app.query_engine.template_loader import SQLTemplate

        template = SQLTemplate(
            id="v2_semantic_query",
            description="Dynamic V2 Semantic Query",
            module="semantic_engine",
            category="query",
            supported_dbs=("mssql", "postgres", "mysql", "oracle", "cloudsql"),
            params={},
            derived_params=(),
            sql_by_dialect={erp_type: generated_sql},
            result_columns=(),
            keywords=(),
            embedding_text=""
        )

        intent = IntentResult(
            template_id="v2_semantic_query",
            params=data.params,
            missing_params=[],
            confidence=1.0,
            rationale="translated_via_yaml_engine"
        )

        # Reconstruct parameter summary for user log
        nl_repr = (
            f"Execute report 'Dynamic V2 Semantic Query' with parameters: {data.params}"
        )
        if session:
            try:
                await session_service.append_turn(db, session, role="user", content=nl_repr)
            except Exception as e:
                log.warning("session_append_user_execute_failed", extra={"err": str(e)})

        # Execute the generated SQL on the connection
        conn = await connection_service.get_connection(db, current, data.connection_id)
        db_type = conn.db_type.value

        from app.query_engine.parameter_binder import BoundQuery
        bound = BoundQuery(sql=generated_sql, params={}, db_type=db_type)

        try:
            result = await execute_collect(conn, bound)
        except TargetDBError as e:
            if session:
                try:
                    await session_service.append_turn(
                        db, session, role="assistant", content=f"Database error: {e.message}"
                    )
                    await _record_history(
                        db,
                        session,
                        conn,
                        current,
                        nl,
                        intent,
                        bound.sql,
                        ExecutionStatus.error,
                        error_message=e.message,
                    )
                except Exception as ex:
                    log.warning("record_history_failed_error", extra={"err": str(ex)})
            return ChatResponse(
                type="error",
                message=f"Database error: {e.message}",
                sql=bound.sql,
                template_id=template.id,
            )

        user_name = current.email.split("@")[0] if "@" in current.email else current.email
        summary: str | None = None
        suggestions = ["Show AP invoice list", "Top 10 customers"]
        try:
            summary = await generate_insight(
                intent=intent.model_dump(), rows=result.rows, user_name=user_name
            )
        except Exception as e:
            log.warning("insight_failed", extra={"err": str(e)})

        msg = summary or f"Query executed successfully. Found {result.rows_returned} rows."
        col_names = list(result.columns) if result.columns else []

        if session:
            try:
                await session_service.append_turn(
                    db,
                    session,
                    role="assistant",
                    content=msg,
                    type="executable",
                    sql=bound.sql,
                    rows=result.rows,
                    columns=col_names,
                    rows_returned=result.rows_returned,
                    execution_time_ms=result.execution_time_ms,
                    template_id=template.id,
                    template_description=template.description,
                    extracted_params=intent.params,
                    suggestions=suggestions,
                )
                await _record_history(
                    db,
                    session,
                    conn,
                    current,
                    nl,
                    intent,
                    bound.sql,
                    ExecutionStatus.success,
                    execution_time_ms=result.execution_time_ms,
                    rows_returned=result.rows_returned,
                )
            except Exception as e:
                log.warning("record_history_success_failed", extra={"err": str(e)})

        return ChatResponse(
            type="executable",
            message=msg,
            template_id=template.id,
            template_description=template.description,
            template_module=template.module,
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

    registry = get_template_registry()
    store = get_pinecone_store_optional()

    # Load session if provided
    session = None
    if data.session_id:
        session = await session_service.get(db, current, data.session_id)
        if session.org_id != current.org_id:
            raise Forbidden("Session does not belong to your organization")

    # Try Pinecone first for the template
    template = None
    if store:
        try:
            meta = store.get_template_by_id(data.template_id)
            if meta:
                template = create_template_from_pinecone(meta)
        except Exception:
            pass

    if template is None and registry.has(data.template_id):
        template = registry.get(data.template_id)

    if template is None:
        return ChatResponse(type="error", message=f"Template '{data.template_id}' not found.")

    conn = await connection_service.get_connection(db, current, data.connection_id)
    db_type = conn.db_type.value

    # Reconstruct parameter summary for user log
    nl_repr = (
        f"Execute report '{template.description or template.id}' with parameters: {data.params}"
    )
    if session:
        try:
            await session_service.append_turn(db, session, role="user", content=nl_repr)
        except Exception as e:
            log.warning("session_append_user_execute_failed", extra={"err": str(e)})

    try:
        bound = bind(template, data.params, db_type=db_type)
    except ValidationFailed as e:
        err_msg = f"Parameter error: {e.message}"
        if session:
            try:
                await session_service.append_turn(db, session, role="assistant", content=err_msg)
            except Exception as ex:
                log.warning("session_append_param_error_failed", extra={"err": str(ex)})
        return ChatResponse(
            type="error",
            message=err_msg,
            template_id=template.id,
        )

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as e:
        err_msg = f"Database error: {e.message}"
        if session:
            try:
                intent_obj = IntentResult(
                    template_id=template.id,
                    params=data.params,
                    missing_params=[],
                    confidence=1.0,
                    rationale="Executed via parameters",
                )
                await session_service.append_turn(db, session, role="assistant", content=err_msg)
                await _record_history(
                    db,
                    session,
                    conn,
                    current,
                    nl_repr,
                    intent_obj,
                    bound.sql,
                    ExecutionStatus.error,
                    error_message=e.message,
                )
            except Exception as ex:
                log.warning("record_history_failed_error_execute", extra={"err": str(ex)})
        return ChatResponse(type="error", message=err_msg, sql=bound.sql)

    summary: str | None = None
    suggestions: list[str] = []
    intent_dict = {"template_id": template.id, "params": data.params}
    results_parallel = await asyncio.gather(
        generate_insight(intent=intent_dict, rows=result.rows, user_name=None),
        generate_suggestions(
            template_id=template.id,
            module=template.module,
            category=template.category,
            description=template.description,
            user_name=None,
        ),
        return_exceptions=True,
    )
    if not isinstance(results_parallel[0], Exception):
        summary = results_parallel[0]
    else:
        log.warning("execute_insight_failed", extra={"err": str(results_parallel[0])})
    if not isinstance(results_parallel[1], Exception):
        suggestions = results_parallel[1]
    else:
        log.warning("execute_suggestions_failed", extra={"err": str(results_parallel[1])})

    msg = summary or f"Executed. {result.rows_returned} rows returned."
    col_names = []
    if result.columns:
        col_names = list(result.columns)
    elif template and template.result_columns:
        col_names = list(template.result_columns)

    if not col_names and bound and bound.sql:
        from app.query_engine.semantic_resolver import extract_columns_from_sql
        col_names = extract_columns_from_sql(bound.sql)

    if session:
        try:
            intent_obj = IntentResult(
                template_id=template.id,
                params=data.params,
                missing_params=[],
                confidence=1.0,
                rationale="Executed via parameters",
            )
            await session_service.append_turn(
                db,
                session,
                role="assistant",
                content=msg,
                type="executable",
                sql=bound.sql,
                rows=result.rows,
                columns=col_names,
                rows_returned=result.rows_returned,
                execution_time_ms=result.execution_time_ms,
                template_id=template.id,
                template_description=template.description,
                extracted_params=data.params,
                suggestions=suggestions,
            )
            await _record_history(
                db,
                session,
                conn,
                current,
                nl_repr,
                intent_obj,
                bound.sql,
                ExecutionStatus.success,
                execution_time_ms=result.execution_time_ms,
                rows_returned=result.rows_returned,
            )
        except Exception as e:
            log.warning("record_history_success_execute_failed", extra={"err": str(e)})

    return ChatResponse(
        type="executable",
        message=msg,
        template_id=template.id,
        template_description=template.description,
        template_module=template.module,
        extracted_params=data.params,
        sql=bound.sql,
        rows=result.rows,
        columns=col_names,
        rows_returned=result.rows_returned,
        execution_time_ms=result.execution_time_ms,
        summary=summary,
        suggestions=suggestions,
    )


# ═══════════════════════════════════════════════════════════════════════
# LEGACY: Original REST endpoint (kept for backward compat)
# ═══════════════════════════════════════════════════════════════════════


async def run_via_rest(
    db: AsyncIOMotorDatabase,
    current: CurrentUser,
    *,
    session_id: uuid.UUID,
    natural_language: str,
) -> RunQueryResponse:
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    if s.ENGINE_VERSION.lower().strip() == "v2":
        # Determine the ERP type dynamically (syspro or epicor)
        erp_type = "syspro"
        if conn and conn.name and "epicor" in conn.name.lower():
            erp_type = "epicor"
        else:
            try:
                org = await db["organizations"].find_one({"_id": str(current.org_id)})
                if org and org.get("erpSystem"):
                    system_name = org.get("erpSystem", "").lower()
                    if "epicor" in system_name:
                        erp_type = "epicor"
                    elif "syspro" in system_name:
                        erp_type = "syspro"
            except Exception:
                pass

        from app.query_engine.semantic_resolver import SemanticResolver
        resolver = SemanticResolver(erp_type=erp_type)
        generated_sql = await resolver.translate_to_sql(natural_language)

        from app.query_engine.template_loader import SQLTemplate

        template = SQLTemplate(
            id="v2_semantic_query",
            description="Dynamic V2 Semantic Query",
            module="semantic_engine",
            category="query",
            supported_dbs=("mssql", "postgres", "mysql", "oracle", "cloudsql"),
            params={},
            derived_params=(),
            sql_by_dialect={erp_type: generated_sql},
            result_columns=(),
            keywords=(),
            embedding_text=""
        )

        intent = IntentResult(
            template_id="v2_semantic_query",
            params={},
            missing_params=[],
            confidence=1.0,
            rationale="translated_via_yaml_engine"
        )

        from app.query_engine.parameter_binder import BoundQuery
        bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)
    else:
        intent = await _intent(natural_language, session.context_window)
        template = get_template_registry().get(intent.template_id)
        bound = bind(template, intent.params, db_type=conn.db_type.value)

    try:
        result = await execute_collect(conn, bound)
    except TargetDBError as e:
        await _record_history(
            db,
            session,
            conn,
            current,
            natural_language,
            intent,
            bound.sql,
            ExecutionStatus.error,
            error_message=e.message,
        )
        raise

    history = await _record_history(
        db,
        session,
        conn,
        current,
        natural_language,
        intent,
        bound.sql,
        ExecutionStatus.success,
        execution_time_ms=result.execution_time_ms,
        rows_returned=result.rows_returned,
    )

    col_names = []
    if result.columns:
        col_names = list(result.columns)
    elif template and template.result_columns:
        col_names = list(template.result_columns)

    await session_service.append_turn(db, session, role="user", content=natural_language)

    summary: str | None = None
    try:
        summary = await generate_insight(
            intent=intent.model_dump(), rows=result.rows, user_name=None
        )
        await session_service.append_turn(db, session, role="assistant", content=summary, columns=col_names)
    except LLMError as e:
        log.warning("insight_failed", extra={"err": str(e)})

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
    """Used by WebSocket. Emits events via `on_event`."""
    session = await session_service.get(db, current, session_id)
    conn = await connection_service.get_connection(db, current, session.connection_id)

    await on_event({"type": "status", "message": "Connecting to database..."})
    await on_event({"type": "progress", "step": "intent_extraction"})

    if s.ENGINE_VERSION.lower().strip() == "v2":
        # Determine the ERP type dynamically (syspro or epicor)
        erp_type = "syspro"
        if conn and conn.name and "epicor" in conn.name.lower():
            erp_type = "epicor"
        else:
            try:
                org = await db["organizations"].find_one({"_id": str(current.org_id)})
                if org and org.get("erpSystem"):
                    system_name = org.get("erpSystem", "").lower()
                    if "epicor" in system_name:
                        erp_type = "epicor"
                    elif "syspro" in system_name:
                        erp_type = "syspro"
            except Exception:
                pass

        from app.query_engine.semantic_resolver import SemanticResolver
        resolver = SemanticResolver(erp_type=erp_type)
        generated_sql = await resolver.translate_to_sql(natural_language)

        from app.query_engine.template_loader import SQLTemplate

        template = SQLTemplate(
            id="v2_semantic_query",
            description="Dynamic V2 Semantic Query",
            module="semantic_engine",
            category="query",
            supported_dbs=("mssql", "postgres", "mysql", "oracle", "cloudsql"),
            params={},
            derived_params=(),
            sql_by_dialect={erp_type: generated_sql},
            result_columns=(),
            keywords=(),
            embedding_text=""
        )

        intent = IntentResult(
            template_id="v2_semantic_query",
            params={},
            missing_params=[],
            confidence=1.0,
            rationale="translated_via_yaml_engine"
        )

        from app.query_engine.parameter_binder import BoundQuery
        bound = BoundQuery(sql=generated_sql, params={}, db_type=conn.db_type.value)
    else:
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
            db,
            session,
            conn,
            current,
            natural_language,
            intent,
            bound.sql,
            ExecutionStatus.error,
            error_message=e.message,
        )
        raise

    exec_ms = int((time.perf_counter() - started) * 1000)
    history = await _record_history(
        db,
        session,
        conn,
        current,
        natural_language,
        intent,
        bound.sql,
        ExecutionStatus.success,
        execution_time_ms=exec_ms,
        rows_returned=rows_returned,
    )
    await session_service.append_turn(db, session, role="user", content=natural_language)

    col_names = []
    if sample:
        col_names = list(sample[0].keys())
    elif template and template.result_columns:
        col_names = list(template.result_columns)

    if not col_names and bound and bound.sql:
        from app.query_engine.semantic_resolver import extract_columns_from_sql
        col_names = extract_columns_from_sql(bound.sql)

    await on_event({"type": "progress", "step": "insight"})
    try:
        summary = await generate_insight(intent=intent.model_dump(), rows=sample)
        await session_service.append_turn(db, session, role="assistant", content=summary, columns=col_names)
        await on_event({"type": "insight", "summary": summary})
    except LLMError as e:
        log.warning("insight_failed", extra={"err": str(e)})

    return {
        "history_id": str(history.id),
        "rows_returned": rows_returned,
        "exec_time_ms": exec_ms,
        "columns": col_names,
    }


# ── private helpers ──────────────────────────────────────────────────


_MODULE_KEYWORDS: dict[str, list[str]] = {
    "ar": [
        "accounts receivable", "receivable", "customer", "debtor", "sales invoice",
        "ar aging", "ar ageing", "customer invoice", "customer balance", "credit control",
        "ar"
    ],
    "ap": [
        "accounts payable", "payable", "supplier", "vendor", "purchase invoice",
        "ap aging", "ap ageing", "supplier invoice", "creditor", "bill",
        "ap"
    ],
    "cashbook": [
        "cashbook", "cash book", "bank account", "bank balance", "cash transaction", "petty cash"
    ],
    "gl": [
        "general ledger", "gl", "trial balance", "journal", "p&l", "profit and loss", "balance sheet"
    ],
    "sorder": [
        "sales order", "sales orders", "so ", "so_"
    ],
    "sinvoice": [
        "sales invoice", "invoice sales", "customer invoice"
    ],
    "dispatch": [
        "dispatch", "delivery note", "shipment", "shipping log", "shipped"
    ],
    "porder": [
        "purchase order", "purchase orders", "po ", "po_"
    ],
    "pinvoice": [
        "purchase invoice", "supplier bill", "vendor invoice"
    ],
    "grn": [
        "grn", "goods received", "goods receipt", "receipt note"
    ],
    "bom": [
        "bill of material", "bill of materials", "bom", "product structure", "component list", "raw material"
    ],
    "wip": [
        "work in progress", "wip", "semi finished", "production status", "active production"
    ],
    "jobcosting": [
        "job costing", "job cost", "cost variance", "actual vs budget", "job profitability", "job order cost"
    ],
    "invvaluation": [
        "inventory valuation", "stock valuation", "valuation", "item cost", "inventory cost"
    ],
    "invholding": [
        "inventory holding", "stock on hand", "warehouse", "bin location", "slow moving", "dead stock", "reorder"
    ],
    "finance": [
        "finance", "financial", "accounting", "ledger"
    ],
    "sales": [
        "sales", "sold", "sales report", "revenue"
    ],
    "purchase": [
        "purchase", "purchasing", "procurement"
    ],
    "manufacturing": [
        "manufacturing", "production", "work order", "production order"
    ],
    "inventory": [
        "inventory", "stock", "warehouse"
    ],
}


_MODULE_KEY_TO_PARENT: dict[str, str] = {
    "ar": "finance",
    "ap": "finance",
    "cashbook": "finance",
    "gl": "finance",
    "finance": "finance",
    "sorder": "sales",
    "sinvoice": "sales",
    "dispatch": "sales",
    "sales": "sales",
    "porder": "purchase",
    "pinvoice": "purchase",
    "grn": "purchase",
    "purchase": "purchase",
    "bom": "manufacturing",
    "wip": "manufacturing",
    "jobcosting": "manufacturing",
    "manufacturing": "manufacturing",
    "invvaluation": "inventory",
    "invholding": "inventory",
    "inventory": "inventory",
}


_ERP_MODULE_ROLES: dict[str, list[str]] = {
    "finance":       ["admin", "editor", "viewer"],
    "sales":         ["admin", "editor", "viewer"],
    "inventory":     ["admin", "editor", "viewer"],
    "purchase":      ["admin", "editor"],
    "manufacturing": ["admin", "editor"],
}


_ERP_MODULE_DISPLAY: dict[str, str] = {
    "finance":       "Finance",
    "sales":         "Sales",
    "inventory":     "Inventory",
    "purchase":      "Purchase",
    "manufacturing": "Manufacturing",
}

_SUB_MODULE_DISPLAY: dict[str, str] = {
    "ar": "Accounts Receivable (AR)",
    "ap": "Accounts Payable (AP)",
    "cashbook": "Cashbook",
    "gl": "General Ledger (GL)",
    "sorder": "Sales Order (S Order)",
    "sinvoice": "Sales Invoice (S Invoice)",
    "dispatch": "Dispatch",
    "porder": "Purchase Order (P Order)",
    "pinvoice": "Purchase Invoice (P Invoice)",
    "grn": "Goods Received Note (GRN)",
    "bom": "Bill of Material (BOM)",
    "wip": "Work in Progress (WIP)",
    "jobcosting": "Job Costing",
    "invvaluation": "Inventory Valuation",
    "invholding": "Inventory Holding",
}


def _check_module_access(module_key: str | None, current: CurrentUser) -> tuple[bool, str]:
    if module_key is None:
        return True, ""

    parent = _MODULE_KEY_TO_PARENT.get(module_key)
    if parent is None:
        return True, ""

    display_name = _SUB_MODULE_DISPLAY.get(module_key) or _ERP_MODULE_DISPLAY.get(module_key) or module_key.title()

    if current.module_permissions is not None:
        override = current.module_permissions.get(module_key)
        if override is not None:
            if override:
                return True, ""
            else:
                return False, f"Access denied: Your account has been explicitly restricted from accessing {display_name}."

        if module_key != parent:
            parent_override = current.module_permissions.get(parent)
            if parent_override is not None:
                if parent_override:
                    return True, ""
                else:
                    parent_display = _ERP_MODULE_DISPLAY.get(parent, parent.title())
                    return False, f"Access denied: Your account has been explicitly restricted from accessing the {parent_display} module (which includes {display_name})."

    allowed_roles = _ERP_MODULE_ROLES.get(parent, [])
    if current.role not in allowed_roles:
        parent_display = _ERP_MODULE_DISPLAY.get(parent, parent.title())
        return False, (
            f"Access denied: Your role ('{current.role}') does not have permission "
            f"to run queries in the {parent_display} module (which includes {display_name}). "
            f"Contact your administrator to request elevated access."
        )
    return True, ""


def _detect_module_from_query(query: str) -> str | None:
    import re
    q = query.lower()
    best_module = None
    best_hits = 0

    for module, keywords in _MODULE_KEYWORDS.items():
        hits = 0
        for kw in keywords:
            if len(kw.strip()) <= 3:
                pattern = r"\b" + re.escape(kw.strip()) + r"\b"
                if re.search(pattern, q):
                    hits += 1
            else:
                if kw in q:
                    hits += 1
        if hits > best_hits:
            best_hits = hits
            best_module = module

    return best_module


def _smart_static_fallback(
    registry: TemplateRegistry,
    query: str,
    max_results: int = 10,
) -> list[dict[str, Any]]:
    q_lower = query.lower()
    q_words = set(q_lower.split())
    all_templates = registry.all()

    # Detect the heuristically matched module first to boost it
    detected_module = _detect_module_from_query(query)

    scored = []
    for t in all_templates:
        # Check description overlap
        desc_words = set(t.description.lower().split())
        desc_overlap = len(q_words & desc_words)

        # Check category and module overlap
        category_words = set(t.category.lower().replace("_", " ").split())
        module_words = set(t.module.lower().replace("_", " ").split())
        cat_overlap = len(q_words & category_words)
        mod_overlap = len(q_words & module_words)

        # Check keywords overlap
        kw_hits = 0
        for kw in t.keywords:
            kw_lower = kw.lower()
            if kw_lower in q_lower or any(w in kw_lower for w in q_words):
                kw_hits += 1

        # Check detected module match
        module_match = 1 if detected_module and t.module.lower() == detected_module.lower() else 0

        # Calculate a robust score
        score = (
            desc_overlap * 3
            + cat_overlap * 2
            + mod_overlap * 2
            + kw_hits * 4
            + module_match * 10
        )

        if score > 0:
            scored.append((score, {
                "id": t.id,
                "description": t.description,
                "module": t.module,
                "category": t.category,
                "params": t.params,
                "supported_dbs": list(t.supported_dbs),
            }))

    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in scored[:max_results]]

    # If no match at all, list first max_results formatted templates
    return [
        {
            "id": t.id,
            "description": t.description,
            "module": t.module,
            "category": t.category,
            "params": t.params,
            "supported_dbs": list(t.supported_dbs),
        }
        for t in all_templates[:max_results]
    ]


async def _intent(natural_language: str, ctx: list) -> IntentResult:
    s = get_settings()

    module_filter = _detect_module_from_query(natural_language)

    store = get_pinecone_store_optional()
    if store:
        try:
            candidates = store.search_with_rerank(
                natural_language, top_k=5, module_filter=module_filter
            )
            if candidates:
                intent = await extract_intent(
                    natural_language,
                    template_candidates=candidates,
                    context_window=ctx,
                )
                # Allow execution even with low confidence if a specific template_id was successfully mapped
                if intent.template_id:
                    return intent
        except Exception as e:
            log.warning("pinecone_intent_failed", extra={"err": str(e)})

    registry = get_template_registry()
    catalog = _smart_static_fallback(registry, natural_language)
    intent = await extract_intent(natural_language, template_candidates=catalog, context_window=ctx)
    if not intent.template_id:
        raise ValidationFailed(
            "Could not match a query template",
            suggestions=[t["id"] for t in catalog],
            confidence=intent.confidence,
        )
    return intent


async def _record_history(
    db: AsyncIOMotorDatabase,
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
    h_doc = QueryHistory.new(
        session_id=str(session.id),
        user_id=str(current.user_id),
        org_id=str(current.org_id),
        connection_id=str(conn.id),
        natural_language_input=nl,
        generated_sql=sql,
        row_size=None,
        intent=intent.model_dump(),
        execution_status=status,
        error_message=error_message,
        execution_time_ms=execution_time_ms,
        rows_returned=rows_returned,
    )
    await db[QueryHistory.COLLECTION].insert_one(h_doc)
    return QueryHistory(**h_doc)
