from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.security.auth import CurrentUser
from app.engine.resolver.semantic_resolver import SemanticResolver
from app.schemas.query import ChatRequest, IntentClassification
from app.services.chat.chat_service import chat
from app.services.chat.execute_service import execute_with_params


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
@patch("app.services.chat.chat_service.classify_intent")
@patch("app.services.chat.chat_service.connection_service")
@patch("app.services.chat.chat_service.execute_collect")
@patch("app.services.chat.chat_service.generate_insight")
@patch("app.services.chat.chat_service.SemanticResolver.translate_to_sql")
async def test_chat_date_dependency(
    mock_translate_to_sql,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_classify_intent,
    mock_db,
    current_user,
):
    mock_classify_intent.return_value = IntentClassification(
        type="executable",
        confidence=0.9,
        reasoning="needs execution",
    )

    # Setup connection service mock
    mock_conn = MagicMock()
    mock_conn.db_type.value = "mssql"
    mock_conn.name = "syspro"
    mock_connection_service.get_connection = AsyncMock(return_value=mock_conn)

    # In V2, we query a temporal query without inputting dates:
    # This should trigger detection of date dependency (via DATE_ADD/DATEADD indicator)
    mock_translate_to_sql.return_value = "SELECT * FROM ApInvoice WHERE InvoiceDate >= DATE_ADD(NOW(), INTERVAL -3 MONTH)"

    request = ChatRequest(
        natural_language="show ap invoices",
        connection_id=uuid.uuid4(),
        session_id=None,
    )

    resp = await chat(mock_db, current_user, data=request)

    # Assert that it successfully detected the date dependency and returned params_needed
    assert resp.type == "params_needed"
    assert resp.template_id == "semantic_query"
    assert len(resp.missing_params) == 2
    assert resp.missing_params[0].name == "start_date"
    assert resp.missing_params[1].name == "end_date"


@pytest.mark.asyncio
@patch("app.services.chat.execute_service.connection_service")
@patch("app.services.chat.execute_service.execute_collect")
@patch("app.services.chat.execute_service.generate_insight")
@patch("app.services.chat.execute_service.SemanticResolver.translate_to_sql")
@patch("app.services.chat.execute_service.session_service")
async def test_execute_with_params_v2(
    mock_session_service,
    mock_translate_to_sql,
    mock_gen_insight,
    mock_execute_collect,
    mock_connection_service,
    mock_db,
    current_user,
):
    from app.schemas.query import ExecuteRequest

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
        template_id="semantic_query",
        params={"start_date": "2026-04-03", "end_date": "2026-07-03"},
        connection_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
    )

    resp = await execute_with_params(mock_db, current_user, data=request)

    assert resp.type == "executable"
    assert resp.template_id == "semantic_query"
    # Ensure translate_to_sql was called with the date parameters
    mock_translate_to_sql.assert_called_with("show ap invoices for the last 3 months", start_date="2026-04-03", end_date="2026-07-03")
    mock_execute_collect.assert_called_once()


@pytest.mark.asyncio
@patch("app.engine.resolver.semantic_resolver.get_llm")
async def test_semantic_resolver_sql_extraction(mock_get_llm):
    mock_llm_instance = MagicMock()
    mock_llm_instance.chat_text = AsyncMock(return_value="""Based on the provided schema, here is the query:
```sql
SELECT c.Customer, c.Name
FROM ArCustomer c
WHERE c.TaxStatus = 'T'
```
I hope this helps!""")
    mock_get_llm.return_value = mock_llm_instance

    resolver = SemanticResolver(erp_type="syspro")
    resolver._context_builder = MagicMock()
    resolver._context_builder.load_meta = MagicMock(return_value={"conventions": {"dialect": "mssql"}})
    resolver._context_builder.build = MagicMock(return_value="Mock prompt context")

    sql = await resolver.translate_to_sql("give me all tax paid companies")

    expected_sql = """SELECT c.Customer, c.Name
FROM ArCustomer c
WHERE c.TaxStatus = 'T'"""

    assert sql == expected_sql


@pytest.mark.asyncio
@patch("app.services.chat.chat_service.classify_intent")
@patch("app.services.chat.chat_service.SemanticResolver.translate_to_sql")
async def test_chat_v2_conversational_response(
    mock_translate_to_sql,
    mock_classify_intent,
    mock_db,
    current_user,
):
    mock_classify_intent.return_value = IntentClassification(
        type="executable",
        confidence=0.9,
        reasoning="needs execution",
    )

    mock_translate_to_sql.return_value = "CONVERSATIONAL:I need to clarify your request. It seems like you're asking for..."

    from app.schemas.query import ChatRequest

    request = ChatRequest(
        natural_language="give me most pai dcheuq details",
        connection_id=uuid.uuid4(),
        session_id=None,
    )

    resp = await chat(mock_db, current_user, data=request)

    assert resp.type == "conversational"
    assert resp.message == "I need to clarify your request. It seems like you're asking for..."
    assert len(resp.suggestions) > 0
