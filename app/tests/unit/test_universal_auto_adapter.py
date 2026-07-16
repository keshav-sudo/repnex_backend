import os
import shutil
import uuid
import pytest
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch
from app.core.database.models import DBConnection, DBType
from app.core.security.auth import CurrentUser
from app.services.connection_service import sync_schema
from app.services.adapter_generator_service import generate_and_index_adapters
from app.engine.resolver.context_builder import ContextBuilder
from app.engine.resolver.semantic_resolver import SemanticResolver
from app.engine.loader.erp_registry import V2_DIR

@pytest.fixture
def mock_mongo_db():
    db = AsyncMock()
    # Mock find_one to return the updated connection document
    async def mock_find_one(filter):
        uid = str(uuid.uuid4())
        return {
            "_id": filter.get("_id") or uid,
            "org_id": uid,
            "created_by": uid,
            "name": "Test Mongo DB",
            "db_type": "mongodb",
            "host": "localhost",
            "port": 27017,
            "db_name": "test_db",
            "encrypted_username": "",
            "encrypted_password": "",
            "ssl_enabled": False,
            "is_active": True,
            "last_tested_at": None,
            "created_at": datetime.now(UTC),
            "schema_info": {
                "tables": [
                    {
                        "name": "customers",
                        "columns": [
                            {"name": "_id", "type": "string"},
                            {"name": "name", "type": "string"},
                            {"name": "balance", "type": "double"}
                        ]
                    },
                    {
                        "name": "orders",
                        "columns": [
                            {"name": "_id", "type": "string"},
                            {"name": "customer_id", "type": "string"},
                            {"name": "amount", "type": "double"}
                        ]
                    }
                ]
            }
        }
    mock_connections = AsyncMock()
    mock_connections.find_one = mock_find_one
    mock_connections.update_one = AsyncMock()
    
    mock_semantic_configs = AsyncMock()
    
    def get_item(name):
        if name in ("connections", "db_connections"):
            return mock_connections
        if name == "semantic_configs":
            return mock_semantic_configs
        return AsyncMock()
        
    db.__getitem__.side_effect = get_item
    return db

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
@patch("app.services.connection_service.get_connection")
@patch("motor.motor_asyncio.AsyncIOMotorClient")
async def test_mongodb_sync_schema(mock_motor_client, mock_get_conn, mock_mongo_db, current_user):
    conn_id = uuid.uuid4()
    mock_conn = DBConnection(
        _id=str(conn_id),
        org_id=current_user.org_id,
        created_by=current_user.user_id,
        name="Test Mongo",
        db_type=DBType.mongodb,
        host="localhost",
        port=27017,
        db_name="test_db",
        encrypted_username="",
        encrypted_password="",
        ssl_enabled=False,
        is_active=True,
        last_tested_at=None,
        created_at=datetime.now(UTC)
    )
    mock_get_conn.return_value = mock_conn

    # Setup mock motor client for target DB schema sampling
    mock_client_inst = MagicMock()
    mock_target_db = AsyncMock()
    mock_target_db.list_collection_names.return_value = ["customers", "orders"]
    
    mock_find_cursor_cust = AsyncMock()
    mock_find_cursor_cust.to_list.return_value = [
        {"_id": "cust1", "name": "Acme", "balance": 1500.50}
    ]
    
    mock_find_cursor_ord = AsyncMock()
    mock_find_cursor_ord.to_list.return_value = [
        {"_id": "ord1", "customer_id": "cust1", "amount": 250.00}
    ]

    mock_cust_collection = MagicMock()
    mock_ord_collection = MagicMock()
    
    mock_cust_collection.find.return_value.limit.return_value = mock_find_cursor_cust
    mock_ord_collection.find.return_value.limit.return_value = mock_find_cursor_ord
    
    def get_collection(name):
        if name == "customers":
            return mock_cust_collection
        return mock_ord_collection
        
    mock_target_db.__getitem__.side_effect = get_collection
    mock_client_inst.__getitem__.return_value = mock_target_db
    mock_motor_client.return_value = mock_client_inst

    res = await sync_schema(mock_mongo_db, current_user, conn_id)

    assert res.db_type == "mongodb"
    assert mock_mongo_db["connections"].update_one.called
    update_call_args = mock_mongo_db["connections"].update_one.call_args[0][1]
    tables = update_call_args["$set"]["schema_info"]["tables"]
    print("EXTRACTED TABLES:", tables)
    
    assert len(tables) == 2
    # Verify field extraction and type inference
    cust_table = next(t for t in tables if t["name"] == "customers")
    assert any(c["name"] == "balance" and c["type"] == "double" for c in cust_table["columns"])
    assert any(c["name"] == "name" and c["type"] == "string" for c in cust_table["columns"])

@pytest.mark.asyncio
@patch("app.services.adapter_generator_service.get_connection")
@patch("app.services.adapter_generator_service.get_llm")
@patch("app.services.vector_store_service.upsert_vectors")
@patch("app.services.vector_store_service.delete_vectors_by_connection")
async def test_generate_and_index_adapters(
    mock_del_vectors, mock_upsert_vectors, mock_get_llm, mock_get_conn, mock_mongo_db, current_user
):
    conn_id = uuid.uuid4()
    mock_conn = DBConnection(
        _id=str(conn_id),
        org_id=current_user.org_id,
        created_by=current_user.user_id,
        name="Test Mongo",
        db_type=DBType.mongodb,
        host="localhost",
        port=27017,
        db_name="test_db",
        encrypted_username="",
        encrypted_password="",
        ssl_enabled=False,
        is_active=True,
        last_tested_at=None,
        created_at=datetime.now(UTC),
        schema_info={
            "tables": [
                {
                    "name": "customers",
                    "columns": [
                        {"name": "_id", "type": "string"},
                        {"name": "name", "type": "string"},
                        {"name": "balance", "type": "double"}
                    ]
                }
            ]
        }
    )
    mock_get_conn.return_value = mock_conn

    # Mock LLM mapping output
    mock_llm = AsyncMock()
    mock_llm.chat_json.return_value = {
        "concepts": [
            {
                "concept": "customer",
                "module": "sales",
                "description": "Represents a business customer entity.",
                "synonyms": ["client", "buyer"],
                "adapter": {
                    "concept": "customer",
                    "table": "customers",
                    "alias": "c",
                    "columns": {
                        "name": {"name": "customer_name", "type": "string", "description": "Name of customer"},
                        "balance": {"name": "outstanding_balance", "type": "double", "description": "Outstanding dues"}
                    }
                }
            }
        ],
        "joins": []
    }
    mock_get_llm.return_value = mock_llm

    # Run mapping generation
    res = await generate_and_index_adapters(mock_mongo_db, current_user, conn_id)

    assert res["status"] == "success"
    assert res["concepts_count"] == 1
    assert res["vectors_indexed"] > 0
    assert mock_upsert_vectors.called
    assert mock_del_vectors.called

    # Clean up generated directories
    conn_str = str(conn_id)
    shutil.rmtree(V2_DIR / "adapters" / conn_str, ignore_errors=True)
    shutil.rmtree(V2_DIR / "ontology" / conn_str, ignore_errors=True)
    shutil.rmtree(V2_DIR / "relationships" / conn_str, ignore_errors=True)

@pytest.mark.asyncio
@patch("app.services.vector_store_service.search_relevant_schema")
async def test_context_builder_rag_pruning(mock_search_schema):
    conn_id = str(uuid.uuid4())
    
    # 1. Setup local adapter files for testing
    adapters_dir = V2_DIR / "adapters" / conn_id
    ontology_dir = V2_DIR / "ontology" / conn_id
    relationships_dir = V2_DIR / "relationships" / conn_id
    
    os.makedirs(adapters_dir, exist_ok=True)
    os.makedirs(ontology_dir, exist_ok=True)
    os.makedirs(relationships_dir, exist_ok=True)
    
    # Write meta
    import yaml
    meta_data = {
        "tables": {
            "customers": {"alias": "c", "columns": ["name", "balance"]},
            "orders": {"alias": "o", "columns": ["amount"]}
        }
    }
    with open(adapters_dir / "_meta.yaml", "w") as f:
        yaml.dump(meta_data, f)
        
    # Write customer adapter/ontology
    with open(adapters_dir / "customer.yaml", "w") as f:
        yaml.dump({"concept": "customer", "table": "customers", "alias": "c"}, f)
    with open(ontology_dir / "customer.yaml", "w") as f:
        yaml.dump({"concept": "customer", "module": "sales", "description": "Customer info"}, f)
        
    # Write order adapter/ontology
    with open(adapters_dir / "order.yaml", "w") as f:
        yaml.dump({"concept": "order", "table": "orders", "alias": "o"}, f)
    with open(ontology_dir / "order.yaml", "w") as f:
        yaml.dump({"concept": "order", "module": "sales", "description": "Orders info"}, f)

    # 2. Mock RAG search to return ONLY customer
    mock_search_schema.return_value = [
        {"metadata": {"table_name": "customers", "concept": "customer"}}
    ]

    builder = ContextBuilder(conn_id)
    # Perform RAG-pruned build
    context = await builder.build(natural_language_query="show customer balances")

    # Verify order table is pruned from the context
    assert "Table: customers" in context
    assert "Concept: customer" in context
    assert "Table: orders" not in context
    assert "Concept: order" not in context

    # 3. Clean up
    shutil.rmtree(adapters_dir, ignore_errors=True)
    shutil.rmtree(ontology_dir, ignore_errors=True)
    shutil.rmtree(relationships_dir, ignore_errors=True)
