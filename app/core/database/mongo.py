"""
app/core/database/mongo.py
──────────────────────────
Production-grade Motor async MongoDB client.
- Singleton engine with configurable pool
- ensure_indexes() wired into FastAPI lifespan
- All collections named to match former PostgreSQL tables
"""
from __future__ import annotations

import asyncio
import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from pymongo import ASCENDING, DESCENDING, IndexModel
from pymongo.errors import AutoReconnect, ServerSelectionTimeoutError

log = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_db: AsyncIOMotorDatabase | None = None


def init_mongo(uri: str, db_name: str, min_pool: int = 5, max_pool: int = 50) -> None:
    """Call once at startup (inside lifespan)."""
    global _client, _db

    kwargs = {
        "minPoolSize": min_pool,
        "maxPoolSize": max_pool,
        "serverSelectionTimeoutMS": 30_000,
        "connectTimeoutMS": 20_000,
        "socketTimeoutMS": 60_000,
        "retryWrites": True,
        "retryReads": True,
    }

    try:
        import certifi
        kwargs["tlsCAFile"] = certifi.where()
        log.info("Using certifi CA bundle for MongoDB connection")
    except ImportError:
        log.warning("certifi not installed, relying on system SSL certificates")

    _client = AsyncIOMotorClient(uri, **kwargs)
    _db = _client[db_name]
    log.info("mongo_client_initialized", extra={"db": db_name, "max_pool": max_pool})


def get_db() -> AsyncIOMotorDatabase:
    if _db is None:
        raise RuntimeError("MongoDB not initialized — call init_mongo() first")
    return _db


async def close_mongo() -> None:
    global _client, _db
    if _client is not None:
        _client.close()
        _client = None
        _db = None
        log.info("mongo_client_closed")


# ── Index Definitions (Production-Grade) ──────────────────────────────────────
async def ensure_indexes(database: AsyncIOMotorDatabase) -> None:
    """
    Idempotent index creation — safe to call on every startup.
    Mirrors PostgreSQL indexes from the SQLAlchemy models exactly,
    plus adds MongoDB-specific compound and TTL indexes.
    """

    max_retries = 5
    backoff = 1.0
    for attempt in range(1, max_retries + 1):
        try:
            # ── organizations ──────────────────────────────────────────────────────────
            await database["organizations"].create_indexes([
                IndexModel([("name", ASCENDING)], unique=True, name="uq_org_name"),
                IndexModel([("owner_id", ASCENDING)], name="ix_org_owner_id", sparse=True),
                IndexModel([("created_at", DESCENDING)], name="ix_org_created_at"),
            ])

            # ── users ──────────────────────────────────────────────────────────────────
            await database["users"].create_indexes([
                # Unique email per org
                IndexModel([("org_id", ASCENDING), ("email", ASCENDING)],
                           unique=True, name="uq_users_org_email"),
                IndexModel([("email", ASCENDING)], name="ix_users_email"),
                IndexModel([("org_id", ASCENDING)], name="ix_users_org_id"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_users_org_created_at"),
                # Fast role + status queries for admin panels
                IndexModel([("org_id", ASCENDING), ("role", ASCENDING)],
                           name="ix_users_org_role"),
                IndexModel([("org_id", ASCENDING), ("status", ASCENDING)],
                           name="ix_users_org_status"),
            ])

            # ── db_connections ─────────────────────────────────────────────────────────
            await database["db_connections"].create_indexes([
                IndexModel([("org_id", ASCENDING)], name="ix_db_connections_org_id"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_db_connections_org_created_at"),
                IndexModel([("org_id", ASCENDING), ("is_active", ASCENDING)],
                           name="ix_db_connections_org_active"),
            ])

            # ── db_connection_access ───────────────────────────────────────────────────
            await database["db_connection_access"].create_indexes([
                IndexModel([("connection_id", ASCENDING), ("user_id", ASCENDING)],
                           unique=True, sparse=True, name="uq_dca_conn_user"),
                IndexModel([("connection_id", ASCENDING)], name="ix_dca_connection_id"),
                IndexModel([("connection_id", ASCENDING), ("org_id", ASCENDING), ("user_id", ASCENDING)],
                           name="ix_dca_connection_org_user"),
                IndexModel([("org_id", ASCENDING), ("user_id", ASCENDING)],
                           name="ix_dca_org_user"),
            ])

            # ── gi_sessions ────────────────────────────────────────────────────────────
            await database["gi_sessions"].create_indexes([
                IndexModel([("user_id", ASCENDING)], name="ix_gi_sessions_user_id"),
                IndexModel([("org_id", ASCENDING), ("user_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_gi_sessions_org_user_created_at"),
                IndexModel([("org_id", ASCENDING), ("status", ASCENDING)],
                           name="ix_gi_sessions_org_status"),
            ])

            # ── query_history ──────────────────────────────────────────────────────────
            await database["query_history"].create_indexes([
                IndexModel([("session_id", ASCENDING)], name="ix_query_history_session_id"),
                IndexModel([("user_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_query_history_user_created"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_query_history_org_created"),
                IndexModel([("execution_status", ASCENDING)], name="ix_query_history_exec_status"),
            ])

            # ── reports ────────────────────────────────────────────────────────────────
            await database["reports"].create_indexes([
                IndexModel([("org_id", ASCENDING)], name="ix_reports_org_id"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_reports_org_created_at"),
                IndexModel([("next_refresh_at", ASCENDING), ("refresh_interval_days", ASCENDING)],
                           name="ix_reports_next_refresh_due", sparse=True),
                IndexModel([("org_id", ASCENDING), ("is_pinned", DESCENDING)],
                           name="ix_reports_org_pinned"),
                IndexModel([("org_id", ASCENDING), ("is_public", ASCENDING)],
                           name="ix_reports_org_public"),
                IndexModel([("query_template_id", ASCENDING)], name="ix_reports_template_id"),
            ])

            # ── report_columns ─────────────────────────────────────────────────────────
            await database["report_columns"].create_indexes([
                IndexModel([("report_id", ASCENDING), ("position", ASCENDING)],
                           name="ix_report_columns_report_position"),
            ])

            # ── report_snapshots ───────────────────────────────────────────────────────
            await database["report_snapshots"].create_indexes([
                IndexModel([("report_id", ASCENDING)], name="ix_report_snapshots_report_id"),
                IndexModel([("org_id", ASCENDING)], name="ix_report_snapshots_org_id"),
                IndexModel([("report_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_report_snapshots_report_created_at"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_report_snapshots_org_created_at"),
                # Auto-purge snapshots older than 180 days (TTL index on created_at)
                IndexModel([("created_at", ASCENDING)],
                           expireAfterSeconds=180 * 24 * 3600,
                           name="ttl_report_snapshots_180d"),
            ])

            # ── dashboards ─────────────────────────────────────────────────────────────
            await database["dashboards"].create_indexes([
                IndexModel([("org_id", ASCENDING)], name="ix_dashboards_org_id"),
                IndexModel([("org_id", ASCENDING), ("created_at", DESCENDING)],
                           name="ix_dashboards_org_created_at"),
                IndexModel([("org_id", ASCENDING), ("is_default", DESCENDING)],
                           name="ix_dashboards_org_default"),
            ])

            # ── dashboard_reports ──────────────────────────────────────────────────────
            await database["dashboard_reports"].create_indexes([
                IndexModel([("dashboard_id", ASCENDING), ("report_id", ASCENDING)],
                           unique=True, name="uq_dr_dash_report"),
                IndexModel([("report_id", ASCENDING)], name="ix_dashboard_reports_report_id"),
            ])

            # ── permission_requests ────────────────────────────────────────────────────
            await database["permission_requests"].create_indexes([
                IndexModel([("org_id", ASCENDING)], name="ix_permission_requests_org_id"),
                IndexModel([("user_id", ASCENDING)], name="ix_permission_requests_user_id"),
                IndexModel([("org_id", ASCENDING), ("status", ASCENDING)],
                           name="ix_permission_requests_org_status"),
                # Unique pending request per user+org+module to prevent duplicates
                IndexModel(
                    [("org_id", ASCENDING), ("user_id", ASCENDING), ("module_key", ASCENDING), ("status", ASCENDING)],
                    name="ix_permission_requests_dedup",
                ),
            ])

            log.info("mongo_indexes_ensured")
            return
        except (AutoReconnect, ServerSelectionTimeoutError) as e:
            if attempt == max_retries:
                log.error(f"Failed to ensure indexes after {max_retries} attempts: {e}")
                raise
            log.warning(
                f"Database connection error during index creation (attempt {attempt}/{max_retries}): {e}. "
                f"Retrying in {backoff}s..."
            )
            await asyncio.sleep(backoff)
            backoff *= 2.0
