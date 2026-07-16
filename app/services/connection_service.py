from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

from app.core.database.models import (
    DBConnection as DBConnectionModel,
)
from app.core.database.models import (
    DBConnectionAccess as DBConnectionAccessModel,
)
from app.core.database.models import (
    DBType,
)
from app.core.database.target_pool import get_target_pool_registry
from app.core.exceptions import Forbidden, NotFound
from app.core.security.auth import CurrentUser
from app.core.security.encryption import decrypt, encrypt
from app.schemas.connection import (
    AccessGrantRead,
    AccessGrantRequest,
    ConnectionCreate,
    ConnectionRead,
    ConnectionUpdate,
    ListDatabasesRequest,
    ListDatabasesResponse,
    TestConnectionResponse,
)
from motor.motor_asyncio import AsyncIOMotorDatabase


def _build_mongo_uri(host: str, port: int, db_name: str, username: str, password: str) -> str:
    from urllib.parse import quote_plus
    
    if "mongodb+srv://" in host or "mongodb://" in host:
        return host
        
    is_srv = ".mongodb.net" in host or not port or port == 0
    scheme = "mongodb+srv" if is_srv else "mongodb"
    port_str = "" if is_srv else f":{port or 27017}"
    
    if username and password:
        return f"{scheme}://{quote_plus(username)}:{quote_plus(password)}@{host}{port_str}/{db_name or ''}"
    else:
        return f"{scheme}://{host}{port_str}/{db_name or ''}"


async def list_connections(
    db: AsyncIOMotorDatabase, current: CurrentUser
) -> list[ConnectionRead]:
    if current.role == "admin":
        cursor = db[DBConnectionModel.COLLECTION].find({"org_id": str(current.org_id)})
    else:
        # Get connection IDs that this non-admin has access to
        access_cursor = db[DBConnectionAccessModel.COLLECTION].find({
            "org_id": str(current.org_id),
            "$or": [
                {"user_id": str(current.user_id)},
                {"user_id": None}
            ]
        })
        access_docs = await access_cursor.to_list(length=1000)
        conn_ids = [doc["connection_id"] for doc in access_docs]
        cursor = db[DBConnectionModel.COLLECTION].find({
            "_id": {"$in": conn_ids},
            "org_id": str(current.org_id)
        })

    rows = await cursor.sort("created_at", -1).to_list(length=1000)
    return [ConnectionRead.model_validate(DBConnectionModel(**r)) for r in rows]


async def get_connection(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID
) -> DBConnectionModel:
    conn_id_str = str(conn_id)
    if current.role == "admin":
        conn = await db[DBConnectionModel.COLLECTION].find_one({
            "_id": conn_id_str,
            "org_id": str(current.org_id)
        })
    else:
        access = await db[DBConnectionAccessModel.COLLECTION].find_one({
            "connection_id": conn_id_str,
            "org_id": str(current.org_id),
            "$or": [
                {"user_id": str(current.user_id)},
                {"user_id": None}
            ]
        })
        if access:
            conn = await db[DBConnectionModel.COLLECTION].find_one({
                "_id": conn_id_str,
                "org_id": str(current.org_id)
            })
        else:
            conn = None

    if not conn:
        raise NotFound("Connection not found")
    return DBConnectionModel(**conn)


async def get_connection_by_id(
    db: AsyncIOMotorDatabase, conn_id: uuid.UUID
) -> DBConnectionModel:
    conn = await db[DBConnectionModel.COLLECTION].find_one({"_id": str(conn_id)})
    if not conn:
        raise NotFound(f"Connection {conn_id} not found")
    return DBConnectionModel(**conn)


async def create_connection(
    db: AsyncIOMotorDatabase, current: CurrentUser, data: ConnectionCreate
) -> ConnectionRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot create connections")

    conn_doc = DBConnectionModel.new(
        org_id=str(current.org_id),
        created_by=str(current.user_id),
        name=data.name,
        db_type=DBType(data.db_type),
        host=data.host,
        port=data.port,
        db_name=data.db_name,
        encrypted_username=encrypt(data.username),
        encrypted_password=encrypt(data.password),
        ssl_enabled=data.ssl_enabled,
        is_active=True,
    )
    await db[DBConnectionModel.COLLECTION].insert_one(conn_doc)

    access_doc = DBConnectionAccessModel.new(
        connection_id=conn_doc["_id"],
        user_id=None,  # whole org by default
        org_id=str(current.org_id),
        granted_by=str(current.user_id),
    )
    await db[DBConnectionAccessModel.COLLECTION].insert_one(access_doc)

    return ConnectionRead.model_validate(DBConnectionModel(**conn_doc))


async def update_connection(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID, data: ConnectionUpdate
) -> ConnectionRead:
    if current.role == "viewer":
        raise Forbidden("Viewers cannot update connections")
    conn = await get_connection(db, current, conn_id)

    payload = data.model_dump(exclude_unset=True)
    update_fields = {}
    if "username" in payload:
        update_fields["encrypted_username"] = encrypt(payload.pop("username"))
    if "password" in payload:
        update_fields["encrypted_password"] = encrypt(payload.pop("password"))
    for k, v in payload.items():
        update_fields[k] = v

    if update_fields:
        await db[DBConnectionModel.COLLECTION].update_one(
            {"_id": str(conn_id)},
            {"$set": update_fields}
        )
        # Fetch updated doc
        updated_doc = await db[DBConnectionModel.COLLECTION].find_one({"_id": str(conn_id)})
        conn = DBConnectionModel(**updated_doc)

    await get_target_pool_registry().evict(conn_id)
    return ConnectionRead.model_validate(conn)


async def delete_connection(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can delete connections")
    conn = await get_connection(db, current, conn_id)

    await db[DBConnectionModel.COLLECTION].delete_one({"_id": str(conn_id)})
    # Clean up associated access records
    await db[DBConnectionAccessModel.COLLECTION].delete_many({"connection_id": str(conn_id)})

    await get_target_pool_registry().evict(conn_id)


async def test_connection(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID
) -> TestConnectionResponse:
    conn = await get_connection(db, current, conn_id)
    started = time.perf_counter()
    if conn.db_type == DBType.mongodb:
        from motor.motor_asyncio import AsyncIOMotorClient
        
        enc_user = getattr(conn, "encrypted_username", "")
        enc_pass = getattr(conn, "encrypted_password", "")
        username = decrypt(enc_user) if enc_user else ""
        password = decrypt(enc_pass) if enc_pass else ""
        
        mongo_uri = _build_mongo_uri(conn.host, conn.port, conn.db_name, username, password)
        try:
            client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
            await client.admin.command("ping")
            client.close()
        except Exception as e:
            return TestConnectionResponse(ok=False, error=f"MongoDB connection failed: {e}")
        
        tested_at = datetime.now(UTC)
        await db[DBConnectionModel.COLLECTION].update_one(
            {"_id": str(conn_id)},
            {"$set": {"last_tested_at": tested_at}}
        )
        return TestConnectionResponse(
            ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
        )

    try:
        pool = await get_target_pool_registry().get_pool(conn)
        await pool.execute_one("SELECT 1 AS ok", {}, timeout=30.0)
    except Exception as e:
        return TestConnectionResponse(ok=False, error=str(e))

    tested_at = datetime.now(UTC)
    await db[DBConnectionModel.COLLECTION].update_one(
        {"_id": str(conn_id)},
        {"$set": {"last_tested_at": tested_at}}
    )
    return TestConnectionResponse(
        ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
    )


async def test_raw_connection(
    current: CurrentUser, data: ConnectionCreate
) -> TestConnectionResponse:
    started = time.perf_counter()
    db_type = DBType(data.db_type)
    if db_type == DBType.mongodb:
        from motor.motor_asyncio import AsyncIOMotorClient
        
        username = data.username or ""
        password = data.password or ""
        
        mongo_uri = _build_mongo_uri(data.host, data.port, data.db_name, username, password)
        try:
            client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
            await client.admin.command("ping")
            client.close()
        except Exception as e:
            return TestConnectionResponse(ok=False, error=f"MongoDB connection failed: {e}")
        return TestConnectionResponse(
            ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
        )

    temp_conn = DBConnectionModel(
        _id=str(uuid.uuid4()),
        org_id=str(current.org_id),
        created_by=str(current.user_id),
        name=data.name,
        db_type=DBType(data.db_type).value,
        host=data.host,
        port=data.port,
        db_name=data.db_name,
        encrypted_username=encrypt(data.username),
        encrypted_password=encrypt(data.password),
        ssl_enabled=data.ssl_enabled,
        is_active=True,
    )
    try:
        registry = get_target_pool_registry()
        pool = await registry._build(temp_conn)
        await pool.execute_one("SELECT 1 AS ok", {}, timeout=30.0)
        await pool.close()
    except Exception as e:
        return TestConnectionResponse(ok=False, error=str(e) or e.__class__.__name__)
    return TestConnectionResponse(
        ok=True, latency_ms=int((time.perf_counter() - started) * 1000)
    )


async def list_databases(
    current: CurrentUser, data: ListDatabasesRequest
) -> ListDatabasesResponse:
    from app.core.database.models import DBType as ModelDBType

    db_type = ModelDBType(data.db_type)

    if db_type == ModelDBType.mssql:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_mssql_databases() -> list[str]:
            import pymssql
            with pymssql.connect(
                server=data.host,
                port=data.port,
                user=data.username,
                password=data.password,
                database="master",
                login_timeout=10,
                timeout=15,
            ) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT name
                        FROM sys.databases
                        WHERE name NOT IN ('tempdb', 'model', 'msdb')
                          AND state_desc = 'ONLINE'
                        ORDER BY name
                        """
                    )
                    return [row[0] for row in cursor.fetchall()]

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=4) as ex:
            try:
                databases = await asyncio.wait_for(
                    loop.run_in_executor(ex, _fetch_mssql_databases),
                    timeout=20.0,
                )
            except TimeoutError:
                raise ValueError("Server did not respond in time — check host/port")
            except Exception as e:
                raise ValueError(f"Cannot connect to server: {e}")
        return ListDatabasesResponse(databases=databases)

    if db_type in (ModelDBType.postgres, ModelDBType.cloudsql):
        import asyncpg
        try:
            conn = await asyncpg.connect(
                host=data.host,
                port=data.port,
                user=data.username,
                password=data.password,
                database="postgres",
                timeout=10,
                ssl="require" if data.ssl_enabled else None,
            )
            rows = await conn.fetch(
                """
                SELECT datname FROM pg_database
                WHERE datistemplate = false
                  AND datallowconn = true
                ORDER BY datname
                """
            )
            await conn.close()
            return ListDatabasesResponse(databases=[r["datname"] for r in rows])
        except Exception as e:
            raise ValueError(f"Cannot connect to server: {e}")

    if db_type == ModelDBType.mysql:
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        def _fetch_mysql_databases() -> list[str]:
            import pymysql
            conn = pymysql.connect(
                host=data.host,
                port=data.port,
                user=data.username,
                password=data.password,
                connect_timeout=10,
            )
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SHOW DATABASES;")
                    return [row[0] for row in cursor.fetchall() if row[0] not in ('information_schema', 'mysql', 'performance_schema', 'sys')]
            finally:
                conn.close()

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=4) as ex:
            try:
                databases = await asyncio.wait_for(
                    loop.run_in_executor(ex, _fetch_mysql_databases),
                    timeout=20.0,
                )
            except TimeoutError:
                raise ValueError("Server did not respond in time — check host/port")
            except Exception as e:
                raise ValueError(f"Cannot connect to server: {e}")
        return ListDatabasesResponse(databases=databases)

    raise ValueError(f"list_databases not supported for db_type: {data.db_type}")


async def grant_access(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID, data: AccessGrantRequest
) -> AccessGrantRead:
    if current.role != "admin":
        raise Forbidden("Only admins can grant access")
    conn = await get_connection(db, current, conn_id)

    grant_doc = DBConnectionAccessModel.new(
        connection_id=str(conn.id),
        user_id=str(data.user_id) if data.user_id else None,
        org_id=str(current.org_id),
        granted_by=str(current.user_id),
    )
    await db[DBConnectionAccessModel.COLLECTION].insert_one(grant_doc)

    return AccessGrantRead.model_validate(DBConnectionAccessModel(**grant_doc))


async def revoke_access(
    db: AsyncIOMotorDatabase, current: CurrentUser, grant_id: uuid.UUID
) -> None:
    if current.role != "admin":
        raise Forbidden("Only admins can revoke access")

    grant = await db[DBConnectionAccessModel.COLLECTION].find_one({
        "_id": str(grant_id),
        "org_id": str(current.org_id),
    })
    if not grant:
        raise NotFound("Grant not found")

    await db[DBConnectionAccessModel.COLLECTION].delete_one({"_id": str(grant_id)})


async def _assert_access(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID
) -> None:
    if current.role == "admin":
        return
    has = await db[DBConnectionAccessModel.COLLECTION].find_one({
        "connection_id": str(conn_id),
        "org_id": str(current.org_id),
        "$or": [
            {"user_id": str(current.user_id)},
            {"user_id": None}
        ]
    })
    if not has:
        raise NotFound("Connection not found")


async def sync_schema(
    db: AsyncIOMotorDatabase, current: CurrentUser, conn_id: uuid.UUID
) -> ConnectionRead:
    conn = await get_connection(db, current, conn_id)

    if conn.db_type == DBType.mongodb:
        from motor.motor_asyncio import AsyncIOMotorClient
        
        enc_user = getattr(conn, "encrypted_username", "")
        enc_pass = getattr(conn, "encrypted_password", "")
        username = decrypt(enc_user) if enc_user else ""
        password = decrypt(enc_pass) if enc_pass else ""
        
        mongo_uri = _build_mongo_uri(conn.host, conn.port, conn.db_name, username, password)
        
        try:
            client = AsyncIOMotorClient(mongo_uri, serverSelectionTimeoutMS=5000)
            m_db = client[conn.db_name or "admin"]
            collections = await m_db.list_collection_names()
            
            tables_list = []
            for coll_name in collections:
                docs = await m_db[coll_name].find().limit(10).to_list(length=10)
                fields_map = {}
                for doc in docs:
                    def parse_doc(d, prefix=""):
                        for k, v in d.items():
                            field_path = f"{prefix}{k}" if not prefix else f"{prefix}.{k}"
                            if isinstance(v, dict):
                                fields_map[field_path] = "object"
                                parse_doc(v, field_path + ".")
                            elif isinstance(v, list):
                                fields_map[field_path] = "array"
                            else:
                                t = type(v).__name__
                                if t == "str":
                                    fields_map[field_path] = "string"
                                elif t == "int":
                                    fields_map[field_path] = "integer"
                                elif t in ("float", "decimal"):
                                    fields_map[field_path] = "double"
                                elif t == "bool":
                                    fields_map[field_path] = "boolean"
                                elif "datetime" in t:
                                    fields_map[field_path] = "datetime"
                                else:
                                    fields_map[field_path] = "string"
                    parse_doc(doc)
                
                columns = [{"name": name, "type": t_name} for name, t_name in fields_map.items()]
                tables_list.append({"name": coll_name, "columns": columns})
            
            await db[DBConnectionModel.COLLECTION].update_one(
                {"_id": str(conn_id)},
                {"$set": {
                    "schema_info": {"tables": tables_list},
                    "schema_last_synced_at": datetime.now(UTC)
                }}
            )
            updated_doc = await db[DBConnectionModel.COLLECTION].find_one({"_id": str(conn_id)})
            conn = DBConnectionModel(**updated_doc)
        except Exception as e:
            raise ValueError(f"MongoDB schema sync failed: {str(e)}")
        
        return ConnectionRead.model_validate(conn)

    pool = await get_target_pool_registry().get_pool(conn)

    if conn.db_type == DBType.postgres or conn.db_type == DBType.cloudsql:
        tables_query = """
            SELECT 
                table_name, 
                column_name, 
                data_type 
            FROM information_schema.columns 
            WHERE table_schema = 'public'
            ORDER BY table_name, ordinal_position;
        """
    elif conn.db_type == DBType.mssql:
        tables_query = """
            SELECT 
                TABLE_NAME, 
                COLUMN_NAME, 
                DATA_TYPE 
            FROM INFORMATION_SCHEMA.COLUMNS 
            WHERE TABLE_SCHEMA = 'dbo'
            ORDER BY TABLE_NAME, ORDINAL_POSITION;
        """
    else:
        tables_query = """
            SELECT 
                TABLE_NAME, 
                COLUMN_NAME, 
                DATA_TYPE 
            FROM INFORMATION_SCHEMA.COLUMNS 
            ORDER BY TABLE_NAME, ORDINAL_POSITION;
        """

    try:
        rows = []
        async for batch in pool.fetch_stream(tables_query, {}, batch_size=1000, timeout=300.0):
            rows.extend(batch)

        tables_map = {}
        for row in rows:
            t_name = row.get("table_name") or row.get("TABLE_NAME") or ""
            c_name = row.get("column_name") or row.get("COLUMN_NAME") or ""
            c_type = row.get("data_type") or row.get("DATA_TYPE") or ""

            if not t_name:
                continue

            if t_name not in tables_map:
                tables_map[t_name] = []
            tables_map[t_name].append({"name": c_name, "type": c_type})

        tables_list = [{"name": name, "columns": cols} for name, cols in tables_map.items()]

        await db[DBConnectionModel.COLLECTION].update_one(
            {"_id": str(conn_id)},
            {"$set": {
                "schema_info": {"tables": tables_list},
                "schema_last_synced_at": datetime.now(UTC)
            }}
        )

        updated_doc = await db[DBConnectionModel.COLLECTION].find_one({"_id": str(conn_id)})
        conn = DBConnectionModel(**updated_doc)
    except Exception as e:
        raise ValueError(f"Schema sync failed: {str(e)}")

    return ConnectionRead.model_validate(conn)
