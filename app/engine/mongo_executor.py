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
    Parse a simple or join SQL SELECT statement into MongoDB parameters.
    """
    sql_clean = re.sub(r"\s+", " ", sql).strip().rstrip(";")
    
    # 1. Parse LIMIT
    limit_match = re.search(r"\bLIMIT\s+(\d+)", sql_clean, re.IGNORECASE)
    limit = _LIMIT_DEFAULT
    if limit_match:
        limit = int(limit_match.group(1))
        sql_clean = sql_clean[:limit_match.start()].strip()
        
    # 2. Parse OFFSET/SKIP
    offset_match = re.search(r"\b(?:SKIP|OFFSET)\s+(\d+)", sql_clean, re.IGNORECASE)
    skip = 0
    if offset_match:
        skip = int(offset_match.group(1))
        sql_clean = sql_clean[:offset_match.start()].strip()

    # 3. Parse ORDER BY
    order_match = re.search(r"\bORDER\s+BY\s+(.+)$", sql_clean, re.IGNORECASE)
    sort_fields = []
    if order_match:
        order_raw = order_match.group(1).strip()
        sql_clean = sql_clean[:order_match.start()].strip()
        parts = [p.strip() for p in order_raw.split(",")]
        for p in parts:
            toks = p.split()
            field = toks[0]
            direction = -1 if len(toks) > 1 and toks[1].upper() == "DESC" else 1
            sort_fields.append((field, direction))

    # 4. Parse WHERE
    where_match = re.search(r"\bWHERE\s+(.+)$", sql_clean, re.IGNORECASE)
    where_clause = None
    if where_match:
        where_clause = where_match.group(1).strip()
        sql_clean = sql_clean[:where_match.start()].strip()

    # 5. Parse SELECT columns
    select_match = re.match(r"\bSELECT\s+(.+?)\s+FROM\s+(.+)$", sql_clean, re.IGNORECASE)
    columns = []
    primary_table = None
    primary_alias = None
    joins = []

    if select_match:
        cols_raw = select_match.group(1).strip()
        rest = select_match.group(2).strip()

        # Parse FROM table and possible alias
        from_part_match = re.match(r"^(\w+)(?:\s+(?:AS\s+)?(\w+))?(.*)$", rest, re.IGNORECASE)
        if from_part_match:
            primary_table = from_part_match.group(1)
            primary_alias = from_part_match.group(2) or primary_table
            joins_raw = from_part_match.group(3).strip()
            
            # Parse Joins
            join_pattern = r"(?:(LEFT|INNER|RIGHT)?\s*JOIN\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?\s+ON\s+([\w\.]+)\s*=\s*([\w\.]+))"
            join_matches = re.finditer(join_pattern, joins_raw, re.IGNORECASE)
            for jm in join_matches:
                j_type = jm.group(1) or "LEFT"
                j_table = jm.group(2)
                j_alias = jm.group(3) or j_table
                left_f = jm.group(4)
                right_f = jm.group(5)
                joins.append({
                    "type": j_type.upper(),
                    "table": j_table,
                    "alias": j_alias,
                    "left_field": left_f,
                    "right_field": right_f
                })

        # Parse columns
        if cols_raw != "*":
            cols_parts = [c.strip() for c in cols_raw.split(",")]
            for part in cols_parts:
                m = re.match(r"(?:(\w+)\.)?(\w+)(?:\s+(?:AS\s+)?(\w+))?", part, re.IGNORECASE)
                if m:
                    tbl_alias = m.group(1)
                    field_name = m.group(2)
                    col_alias = m.group(3) or field_name
                    columns.append({
                        "tbl_alias": tbl_alias,
                        "field": field_name,
                        "alias": col_alias
                    })

    return {
        "collection": primary_table,
        "primary_alias": primary_alias,
        "columns": columns,
        "joins": joins,
        "where_clause": where_clause,
        "sort_fields": sort_fields,
        "limit": limit,
        "skip": skip
    }


def _parse_where(clause: str) -> dict:
    """Very lightweight WHERE → MongoDB filter dict converter supporting dot-notation."""
    mongo_filter: dict[str, Any] = {}

    conditions = re.split(r"\bAND\b", clause, flags=re.IGNORECASE)
    for cond in conditions:
        cond = cond.strip()

        # field IN (v1, v2, ...)
        m = re.match(r"([\w\.]+)\s+IN\s*\((.+)\)", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            vals_raw = m.group(2)
            vals = [v.strip().strip("'\"") for v in vals_raw.split(",")]
            mongo_filter[field] = {"$in": vals}
            continue

        # field NOT IN (...)
        m = re.match(r"([\w\.]+)\s+NOT\s+IN\s*\((.+)\)", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            vals_raw = m.group(2)
            vals = [v.strip().strip("'\"") for v in vals_raw.split(",")]
            mongo_filter[field] = {"$nin": vals}
            continue

        # field LIKE '%val%'
        m = re.match(r"([\w\.]+)\s+LIKE\s+'([^']*)'", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            pattern = m.group(2).replace("%", ".*").replace("_", ".")
            mongo_filter[field] = {"$regex": pattern, "$options": "i"}
            continue

        # field IS NULL / IS NOT NULL
        m = re.match(r"([\w\.]+)\s+IS\s+(NOT\s+)?NULL", cond, re.IGNORECASE)
        if m:
            field = m.group(1)
            is_not = bool(m.group(2))
            mongo_filter[field] = {"$ne": None} if is_not else None
            continue

        # field >= val  /  field <= val  /  field > val  /  field < val  /  field != val  / field = val
        m = re.match(r"([\w\.]+)\s*(>=|<=|!=|<>|>|<|=)\s*('?[^']*'?)", cond)
        if m:
            field = m.group(1)
            op = m.group(2)
            raw_val = m.group(3).strip().strip("'\"")
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

    primary_alias = parsed.get("primary_alias") or collection_name

    try:
        client, db_name = _get_mongo_client_and_db(conn)
        db = client[db_name]

        pipeline = []

        # 1. Match stage (WHERE)
        if parsed["where_clause"]:
            raw_filter = _parse_where(parsed["where_clause"])
            clean_filter = {}
            for k, v in raw_filter.items():
                parts = k.split(".")
                if len(parts) > 1 and parts[0] == primary_alias:
                    clean_key = ".".join(parts[1:])
                else:
                    clean_key = k
                if clean_key == "id":
                    clean_key = "_id"
                clean_filter[clean_key] = v
            if clean_filter:
                pipeline.append({"$match": clean_filter})

        # 2. Lookups and Unwinds for joins
        active_aliases = {primary_alias}
        for j in parsed["joins"]:
            j_alias = j["alias"]
            j_table = j["table"]

            lf_parts = j["left_field"].split(".")
            if lf_parts[-1] == "id":
                lf_parts[-1] = "_id"
            left_f = ".".join(lf_parts)

            rf_parts = j["right_field"].split(".")
            if rf_parts[-1] == "id":
                rf_parts[-1] = "_id"
            right_f = ".".join(rf_parts)

            lf_alias, lf_field = left_f.split(".", 1) if "." in left_f else (None, left_f)
            rf_alias, rf_field = right_f.split(".", 1) if "." in right_f else (None, right_f)

            if lf_alias in active_aliases:
                local_path = lf_field if lf_alias == primary_alias else f"{lf_alias}.{lf_field}"
                foreign_path = rf_field
            else:
                local_path = rf_field if rf_alias == primary_alias else f"{rf_alias}.{rf_field}"
                foreign_path = lf_field

            pipeline.append({
                "$lookup": {
                    "from": j_table,
                    "localField": local_path,
                    "foreignField": foreign_path,
                    "as": j_alias
                }
            })
            pipeline.append({
                "$unwind": {
                    "path": f"${j_alias}",
                    "preserveNullAndEmptyArrays": True
                }
            })
            active_aliases.add(j_alias)

        # 3. Project stage
        project_stage = {}
        if parsed["columns"]:
            for col in parsed["columns"]:
                tbl_alias = col["tbl_alias"]
                field = col["field"]
                alias = col["alias"]
                if field == "id":
                    field = "_id"

                if tbl_alias == primary_alias or not tbl_alias:
                    source_path = f"${field}"
                else:
                    source_path = f"${tbl_alias}.{field}"
                project_stage[alias] = source_path
        
        if project_stage:
            pipeline.append({"$project": project_stage})

        # 4. Sort stage
        if parsed["sort_fields"]:
            sort_stage = {}
            for field, direction in parsed["sort_fields"]:
                f_parts = field.split(".")
                field_name = f_parts[-1]
                if field_name == "id":
                    field_name = "_id"

                projected_alias = None
                for col in parsed["columns"]:
                    c_field = col["field"]
                    if c_field == "id":
                        c_field = "_id"
                    if c_field == field_name and (not col["tbl_alias"] or col["tbl_alias"] == f_parts[0] if len(f_parts) > 1 else True):
                        projected_alias = col["alias"]
                        break
                
                sort_key = projected_alias or field_name
                sort_stage[sort_key] = direction
            if sort_stage:
                pipeline.append({"$sort": sort_stage})

        # 5. Skip and Limit stages
        if parsed["skip"] > 0:
            pipeline.append({"$skip": parsed["skip"]})
        
        limit = min(parsed["limit"], max_rows)
        # pyrefly: ignore [bad-argument-type]
        pipeline.append({"$limit": limit})

        cursor = db[collection_name].aggregate(pipeline)
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
