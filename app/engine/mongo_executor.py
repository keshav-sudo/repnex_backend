"""
mongo_executor.py — Native MongoDB query execution for the Repnex engine.

Accepts the SQL string that the LLM generates (e.g. SELECT * FROM GrnMaster
WHERE status = 'OPEN' LIMIT 50) and converts it into a MongoDB
find() / aggregate() operation so users can query NoSQL databases through
the same chat interface as SQL databases.

Conversion rules:
  SELECT *      FROM <col>                         -> db[col].find()
  SELECT <cols> FROM <col>                         -> db[col].find({}, projection)
  ... WHERE <field> = <val>                         -> filter dict
  ... WHERE <field> IN (v1,v2)                      -> $in
  ... WHERE <field> LIKE '%x%'                      -> $regex
  ... ORDER BY <field> [DESC]                       -> sort
  ... LIMIT <n>                                     -> limit
  ... SKIP / OFFSET <n>                             -> skip

Complex nested JOINs are not supported natively — they fall back to a
collection-scan with a warning comment in the response.
"""
from __future__ import annotations

import re
import time
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.database.models import DBConnection
from app.core.exceptions import TargetDBError
from app.core.logging import get_logger
from app.services.connection_service import _build_mongo_uri
from app.core.security.encryption import decrypt

log = get_logger(__name__)

_LIMIT_DEFAULT = 500


# ──────────────────────────────────────────────────────────────────────────────
# SQL → MongoDB lightweight parser
# ──────────────────────────────────────────────────────────────────────────────

def _parse_sql_to_mongo(sql: str) -> dict:
    """
    Parse a simple SELECT statement into MongoDB find() arguments.
    Returns a dict with keys: collection, projection, filter, sort, limit, skip.
    """
    sql = sql.strip().rstrip(";")

    result: dict[str, Any] = {
        "collection": None,
        "projection": None,
        "filter": {},
        "sort": None,
        "limit": _LIMIT_DEFAULT,
        "skip": 0,
        "is_aggregate": False,
        "pipeline": [],
    }

    # ── LIMIT / SKIP ──────────────────────────────────────────────────────────
    m = re.search(r"\bLIMIT\s+(\d+)", sql, re.IGNORECASE)
    if m:
        result["limit"] = int(m.group(1))
        sql = sql[: m.start()].strip()

    m = re.search(r"\b(?:SKIP|OFFSET)\s+(\d+)", sql, re.IGNORECASE)
    if m:
        result["skip"] = int(m.group(1))
        sql = sql[: m.start()].strip()

    # ── ORDER BY ──────────────────────────────────────────────────────────────
    m = re.search(r"\bORDER\s+BY\s+(.+?)(?:\s+(?:WHERE|LIMIT|SKIP|$))", sql, re.IGNORECASE)
    if not m:
        m = re.search(r"\bORDER\s+BY\s+(.+)$", sql, re.IGNORECASE)
    if m:
        order_raw = m.group(1).strip()
        sql = sql[: m.start()].strip()
        parts = [p.strip() for p in order_raw.split(",")]
        sort_list = []
        for p in parts:
            toks = p.split()
            field = toks[0]
            direction = -1 if len(toks) > 1 and toks[1].upper() == "DESC" else 1
            sort_list.append((field, direction))
        result["sort"] = sort_list

    # ── WHERE ─────────────────────────────────────────────────────────────────
    where_match = re.search(r"\bWHERE\s+(.+?)(?:\s+(?:ORDER|LIMIT|SKIP|$))", sql, re.IGNORECASE)
    if not where_match:
        where_match = re.search(r"\bWHERE\s+(.+)$", sql, re.IGNORECASE)
    if where_match:
        where_clause = where_match.group(1).strip()
        sql = sql[: where_match.start()].strip()
        result["filter"] = _parse_where(where_clause)

    # ── FROM <collection> ─────────────────────────────────────────────────────
    from_match = re.search(r"\bFROM\s+([`\"\[\]]?)(\w+)[`\"\[\]]?", sql, re.IGNORECASE)
    if from_match:
        result["collection"] = from_match.group(2)
        sql = sql[: from_match.start()].strip()

    # ── SELECT <columns> ─────────────────────────────────────────────────────
    select_match = re.match(r"\bSELECT\s+(.+)$", sql, re.IGNORECASE)
    if select_match:
        cols_raw = select_match.group(1).strip()
        if cols_raw != "*":
            cols = [c.strip().strip("`\"[]").split(".")[-1] for c in cols_raw.split(",")]
            # Remove aggregate functions for now
            cols = [c for c in cols if not re.search(r"\(", c)]
            if cols:
                result["projection"] = {c: 1 for c in cols}

    return result


def _parse_where(clause: str) -> dict:
    """Very lightweight WHERE → MongoDB filter dict converter."""
    mongo_filter: dict[str, Any] = {}

    # AND split (simple, not nested)
    conditions = re.split(r"\bAND\b", clause, flags=re.IGNORECASE)
    for cond in conditions:
        cond = cond.strip()

        # field IN (v1, v2, ...)
        m = re.match(r"(\w+)\s+IN\s*\((.+)\)", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            vals_raw = m.group(2)
            vals = [v.strip().strip("'\"") for v in vals_raw.split(",")]
            mongo_filter[field] = {"$in": vals}
            continue

        # field NOT IN (...)
        m = re.match(r"(\w+)\s+NOT\s+IN\s*\((.+)\)", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            vals_raw = m.group(2)
            vals = [v.strip().strip("'\"") for v in vals_raw.split(",")]
            mongo_filter[field] = {"$nin": vals}
            continue

        # field LIKE '%val%'
        m = re.match(r"(\w+)\s+LIKE\s+'([^']*)'", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            pattern = m.group(2).replace("%", ".*").replace("_", ".")
            mongo_filter[field] = {"$regex": pattern, "$options": "i"}
            continue

        # field IS NULL / IS NOT NULL
        m = re.match(r"(\w+)\s+IS\s+(NOT\s+)?NULL", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            is_not = bool(m.group(2))
            mongo_filter[field] = {"$ne": None} if is_not else None
            continue

        # field >= val  /  field <= val  /  field > val  /  field < val  /  field != val  / field = val
        m = re.match(r"(\w+)\s*(>=|<=|!=|<>|>|<|=)\s*('?[^']*'?)", cond)
        if m:
            field = m.group(1)
            op = m.group(2)
            raw_val = m.group(3).strip().strip("'\"")
            # Try to cast to number
            try:
                val: Any = int(raw_val)
            except ValueError:
                try:
                    val = float(raw_val)
                except ValueError:
                    val = raw_val

            op_map = {
                "=": None, ">=": "$gte", "<=": "$lte",
                ">": "$gt", "<": "$lt", "!=": "$ne", "<>": "$ne",
            }
            mongo_op = op_map.get(op)
            mongo_filter[field] = {mongo_op: val} if mongo_op else val
            continue

    return mongo_filter


# ──────────────────────────────────────────────────────────────────────────────
# MongoDB connection builder
# ──────────────────────────────────────────────────────────────────────────────

def _get_mongo_client_and_db(conn: DBConnection):
    """Return (AsyncIOMotorClient, db_name) for the given connection."""
    enc_user = getattr(conn, "encrypted_username", "") or ""
    enc_pass = getattr(conn, "encrypted_password", "") or ""
    username = decrypt(enc_user) if enc_user else ""
    password = decrypt(enc_pass) if enc_pass else ""

    uri = _build_mongo_uri(conn.host, conn.port, conn.db_name, username, password)
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    db_name = conn.db_name or "admin"
    return client, db_name


def clean_mongo_value(val: Any) -> Any:
    if val is None:
        return None
    val_type_str = type(val).__name__
    if val_type_str == "ObjectId":
        return str(val)
    elif isinstance(val, dict):
        return {k: clean_mongo_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_mongo_value(x) for x in val]
    elif hasattr(val, "isoformat"):
        return val.isoformat()
    elif isinstance(val, bytes):
        try:
            return val.decode("utf-8")
        except UnicodeDecodeError:
            return f"0x{val.hex()}"
    return val


async def execute_mongo_collect(
    conn: DBConnection, sql: str, max_rows: int = _LIMIT_DEFAULT
) -> tuple[list[dict], list[str], int]:
    """
    Execute a SQL-like query against a MongoDB database.
    Returns (rows, columns, execution_time_ms).
    """
    started = time.perf_counter()

    parsed = _parse_sql_to_mongo(sql)
    collection_name = parsed.get("collection")
    if not collection_name:
        raise TargetDBError(f"Could not determine collection from query: {sql}")

    log.info(
        "mongo_query_parsed",
        extra={
            "collection": collection_name,
            "filter": str(parsed["filter"]),
            "projection": str(parsed["projection"]),
            "limit": parsed["limit"],
        },
    )

    try:
        client, db_name = _get_mongo_client_and_db(conn)
        db = client[db_name]

        cursor = db[collection_name].find(
            parsed["filter"] or {},
            parsed["projection"] or None,
        )

        if parsed["sort"]:
            cursor = cursor.sort(parsed["sort"])
        if parsed["skip"]:
            cursor = cursor.skip(parsed["skip"])

        limit = min(parsed["limit"], max_rows)
        cursor = cursor.limit(limit)

        raw_rows = await cursor.to_list(length=limit)
        client.close()
    except TargetDBError:
        raise
    except Exception as exc:
        raise TargetDBError(f"MongoDB query failed on '{collection_name}': {exc}") from exc

    # Clean rows — remove ObjectId / convert to str-safe dicts
    rows = [clean_mongo_value(doc) for doc in raw_rows]

    columns = list(rows[0].keys()) if rows else []
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return rows, columns, elapsed_ms
