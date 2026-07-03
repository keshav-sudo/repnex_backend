from __future__ import annotations

import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.schemas.query import ChatRequest, ChatResponse, IntentClassification, IntentResult
from app.core.security.auth import CurrentUser
from app.services.query_service import chat
from app.core.exceptions import ValidationFailed

@pytest.fixture
def mock_db():
    return AsyncMock()

@pytest.fixture
def current_user():
    return CurrentUser(
        user_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        email="test@example.com",
        role="admin",
        module_permissions={},
    )

@pytest.mark.asyncio
@patch("app.services.query_service.classify_intent")
@patch("app.services.query_service.extract_intent")
@patch("app.services.query_service.get_pinecone_store_optional")
@patch("app.services.query_service.get_template_registry")
@patch("app.services.query_service._check_module_access")
@patch("app.services.query_service.connection_service")
@patch("app.services.query_service.execute_collect")
@patch("app.services.query_service.generate_insight")
@patch("app.services.query_service.generate_suggestions")
async def test_chat_low_confidence_still_executes(
    mock_gen_suggestions,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_check_access,
    mock_get_registry,
    mock_get_store,
    mock_extract_intent,
    mock_classify_intent,
    mock_db,
    current_user,
):
    # Setup access check
    mock_check_access.return_value = (True, "")

    # Setup classification
    mock_classify_intent.return_value = IntentClassification(
        type="executable",
        confidence=0.9,
        reasoning="needs execution",
    )

    # Setup pinecone store returning matches
    mock_store = MagicMock()
    mock_store.search_with_rerank.return_value = [
        {
            "id": "ap_ageing_report",
            "description": "AP ageing report",
            "module": "ap",
            "category": "ageing",
            "sql": "SELECT 1",
            "params": {},
            "result_columns": [],
        }
    ]
    mock_get_store.return_value = mock_store

    # Setup registry
    mock_registry = MagicMock()
    mock_registry.has.return_value = False
    mock_get_registry.return_value = mock_registry

    # Setup extract_intent returning LOW confidence but a matched template_id
    mock_extract_intent.return_value = IntentResult(
        template_id="ap_ageing_report",
        params={},
        missing_params=[],
        confidence=0.35,  # below the s.INTENT_MIN_CONFIDENCE of 0.68
        rationale="low confidence match",
    )

    # Setup mock connection and db_type
    mock_conn = MagicMock()
    mock_conn.db_type.value = "postgres"
    mock_connection_service.get_connection = AsyncMock(return_value=mock_conn)

    # Setup mock query results
    mock_result = MagicMock()
    mock_result.rows = []
    mock_result.columns = []
    mock_result.rows_returned = 0
    mock_execute_collect.return_value = mock_result

    # Mock parallel LLM calls
    mock_gen_insight.return_value = "Mocked insight"
    mock_gen_suggestions.return_value = []

    request = ChatRequest(
        natural_language="ap ageing report",
        connection_id=uuid.uuid4(),
        session_id=None,
    )

    resp = await chat(mock_db, current_user, data=request)

    # Assert that it successfully proceeded to execution instead of returning error
    assert resp.type == "executable"
    mock_execute_collect.assert_called_once()


@pytest.mark.asyncio
@patch("app.services.query_service.classify_intent")
@patch("app.services.query_service.extract_intent")
@patch("app.services.query_service.get_pinecone_store_optional")
@patch("app.services.query_service.get_template_registry")
@patch("app.services.query_service._check_module_access")
@patch("app.services.query_service.connection_service")
@patch("app.services.query_service.execute_collect")
@patch("app.services.query_service.generate_insight")
@patch("app.services.query_service.generate_suggestions")
async def test_chat_direct_pinecone_fetch_fallback(
    mock_gen_suggestions,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_check_access,
    mock_get_registry,
    mock_get_store,
    mock_extract_intent,
    mock_classify_intent,
    mock_db,
    current_user,
):
    # Setup access check
    mock_check_access.return_value = (True, "")

    # Setup classification
    mock_classify_intent.return_value = IntentClassification(
        type="executable",
        confidence=0.9,
        reasoning="needs execution",
    )

    # Setup pinecone store returning candidates, but NOT the one the LLM eventually picks
    mock_store = MagicMock()
    mock_store.search_with_rerank.return_value = [
        {
            "id": "some_other_template",
            "description": "Some other template",
            "module": "ap",
            "category": "ageing",
            "sql": "SELECT 2",
            "params": {},
            "result_columns": [],
        }
    ]
    # Configure get_template_by_id to return the template that the LLM picks
    mock_store.get_template_by_id.return_value = {
        "id": "ap_ageing_report",
        "description": "AP ageing report fetched directly",
        "module": "ap",
        "category": "ageing",
        "sql": "SELECT 1",
        "params": {},
        "result_columns": [],
    }
    mock_get_store.return_value = mock_store

    # Setup registry (does not have the template either)
    mock_registry = MagicMock()
    mock_registry.has.return_value = False
    mock_get_registry.return_value = mock_registry

    # Setup extract_intent returning the template ID not present in candidates list
    mock_extract_intent.return_value = IntentResult(
        template_id="ap_ageing_report",
        params={},
        missing_params=[],
        confidence=0.85,
        rationale="high confidence match from context",
    )

    # Setup mock connection and db_type
    mock_conn = MagicMock()
    mock_conn.db_type.value = "postgres"
    mock_connection_service.get_connection = AsyncMock(return_value=mock_conn)

    # Setup mock query results
    mock_result = MagicMock()
    mock_result.rows = []
    mock_result.columns = []
    mock_result.rows_returned = 0
    mock_execute_collect.return_value = mock_result

    # Mock parallel LLM calls
    mock_gen_insight.return_value = "Mocked insight"
    mock_gen_suggestions.return_value = []

    request = ChatRequest(
        natural_language="run ap ageing report",
        connection_id=uuid.uuid4(),
        session_id=None,
    )

    resp = await chat(mock_db, current_user, data=request)

    # Assert that it successfully resolved using direct pinecone fetch and executed
    assert resp.type == "executable"
    mock_store.get_template_by_id.assert_called_with("ap_ageing_report")
    mock_execute_collect.assert_called_once()


@pytest.mark.asyncio
@patch("app.services.query_service.classify_intent")
@patch("app.services.query_service.connection_service")
@patch("app.services.query_service.execute_collect")
@patch("app.services.query_service.generate_insight")
@patch("app.query_engine.semantic_resolver.SemanticResolver.translate_to_sql")
@patch("app.services.query_service.get_settings")
async def test_chat_v2_date_dependency(
    mock_get_settings,
    mock_translate_to_sql,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_classify_intent,
    mock_db,
    current_user,
):
    # Setup settings to use V2 engine
    mock_s = MagicMock()
    mock_s.ENGINE_VERSION = "v2"
    mock_get_settings.return_value = mock_s

    mock_classify_intent.return_value = IntentClassification(
        type="executable",
        confidence=0.9,
        reasoning="needs execution",
    )

    # In V2, we query a temporal query
    mock_translate_to_sql.return_value = "SELECT * FROM ApInvoice WHERE InvoiceDate >= DATEADD(month, -3, GETDATE())"

    request = ChatRequest(
        natural_language="show ap invoices for the last 3 months",
        connection_id=uuid.uuid4(),
        session_id=None,
    )

    resp = await chat(mock_db, current_user, data=request)

    # Assert that it successfully detected the date dependency and returned params_needed
    assert resp.type == "params_needed"
    assert resp.template_id == "v2_semantic_query"
    assert len(resp.missing_params) == 2
    assert resp.missing_params[0].name == "start_date"
    assert resp.missing_params[1].name == "end_date"


@pytest.mark.asyncio
@patch("app.services.query_service.connection_service")
@patch("app.services.query_service.execute_collect")
@patch("app.services.query_service.generate_insight")
@patch("app.query_engine.semantic_resolver.SemanticResolver.translate_to_sql")
@patch("app.services.query_service.get_settings")
@patch("app.services.query_service.session_service")
async def test_execute_with_params_v2(
    mock_session_service,
    mock_get_settings,
    mock_translate_to_sql,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_db,
    current_user,
):
    from app.schemas.query import ExecuteRequest
    from app.services.query_service import execute_with_params

    # Setup settings to use V2 engine
    mock_s = MagicMock()
    mock_s.ENGINE_VERSION = "v2"
    mock_get_settings.return_value = mock_s

    # Setup mock session
    mock_session = MagicMock()
    mock_session.org_id = current_user.org_id
    mock_session.context_window = [
        {"role": "user", "content": "show ap invoices for the last 3 months"}
    ]
    mock_session_service.get = AsyncMock(return_value=mock_session)

    # Setup mock connection and db_type
    mock_conn = MagicMock()
    mock_conn.db_type.value = "postgres"
    mock_connection_service.get_connection = AsyncMock(return_value=mock_conn)

    # Setup mock query results
    mock_result = MagicMock()
    mock_result.rows = []
    mock_result.columns = ["Invoice", "InvoiceDate"]
    mock_result.rows_returned = 0
    mock_execute_collect.return_value = mock_result

    # Mock insight
    mock_gen_insight.return_value = "Mocked insight"

    # Setup mock translate to sql
    mock_translate_to_sql.return_value = "SELECT * FROM ApInvoice WHERE InvoiceDate >= '2026-04-03' AND InvoiceDate <= '2026-07-03'"

    request = ExecuteRequest(
        template_id="v2_semantic_query",
        params={"start_date": "2026-04-03", "end_date": "2026-07-03"},
        connection_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )

    resp = await execute_with_params(mock_db, current_user, data=request)

    assert resp.type == "executable"
    assert resp.template_id == "v2_semantic_query"
    # Ensure translate_to_sql was called with the date parameters
    mock_translate_to_sql.assert_called_with("show ap invoices for the last 3 months", start_date="2026-04-03", end_date="2026-07-03")
    mock_execute_collect.assert_called_once()
