"""SQL Cleaner — post-processing utilities for LLM-generated SQL.

Responsibilities:
- Strip markdown code blocks from raw LLM output
- Translate T-SQL patterns to MySQL equivalents (safety post-processor)
- Extract column names from a SELECT statement
"""
from __future__ import annotations

import re

# ── Markdown stripper ──────────────────────────────────────────────────────────

def clean_llm_sql(raw: str) -> str:
    """Remove markdown code fences from LLM output and return bare SQL."""
    text = raw.strip()

    # Closed fences: ```sql ... ``` or ``` ... ```
    m = re.search(r"```sql\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*?)\s*```", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Unclosed fences (token-limit truncation)
    m = re.search(r"```sql\s*(.*)", text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search(r"```\s*(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip()

    # Partial prefix remnants
    for prefix in ("```sql", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]

    return text.strip()


# ── T-SQL → MySQL post-processor ───────────────────────────────────────────────

def fix_tsql_to_mysql(sql: str) -> str:
    """Convert T-SQL patterns to MySQL equivalents.

    This is a defense-in-depth layer — the LLM prompt should already produce
    correct MySQL, but this catches edge cases from hallucinations.
    """
    # 1. SELECT TOP N ... → SELECT ... LIMIT N
    top_match = re.match(
        r"^\s*(SELECT\s+(?:DISTINCT\s+)?)(TOP\s*\(?\s*(\d+)\s*\)?)\s+",
        sql,
        re.IGNORECASE,
    )
    if top_match:
        limit_n = top_match.group(3)
        sql = re.sub(
            r"^(\s*SELECT\s+(?:DISTINCT\s+)?)TOP\s*\(?\s*\d+\s*\)?\s+",
            r"\1",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        sql = re.sub(r"(;?\s*)$", f" LIMIT {limit_n}\\1", sql.rstrip(), count=1)

    # 2. GETDATE() → NOW()
    sql = re.sub(r"\bGETDATE\s*\(\s*\)", "NOW()", sql, flags=re.IGNORECASE)

    # 3. DATEFROMPARTS(YEAR(NOW()), 1, 1) → DATE_FORMAT(NOW(), '%Y-01-01')
    sql = re.sub(
        r"\bDATEFROMPARTS\s*\(\s*YEAR\s*\(\s*(?:NOW|GETDATE)\s*\(\s*\)\s*\)\s*,\s*1\s*,\s*1\s*\)",
        "DATE_FORMAT(NOW(), '%Y-01-01')",
        sql,
        flags=re.IGNORECASE,
    )

    # 4. DATEADD(interval, n, date) → DATE_ADD(date, INTERVAL n interval)
    def _dateadd_to_mysql(m: re.Match) -> str:
        interval = m.group(1).strip().upper()
        n = m.group(2).strip()
        date_expr = m.group(3).strip()
        return f"DATE_ADD({date_expr}, INTERVAL {n} {interval})"

    sql = re.sub(
        r"\bDATEADD\s*\(\s*(\w+)\s*,\s*(-?\d+)\s*,\s*([^()]+(?:\([^()]*\)[^()]*)*)\)",
        _dateadd_to_mysql,
        sql,
        flags=re.IGNORECASE,
    )

    # 5. Square bracket identifiers [ColumnName] → ColumnName
    sql = re.sub(r"\[([^\]]+)\]", r"\1", sql)

    # 6. ISNULL(a, b) → IFNULL(a, b)
    sql = re.sub(r"\bISNULL\s*\(", "IFNULL(", sql, flags=re.IGNORECASE)

    # 7. LEN(x) → CHAR_LENGTH(x)
    sql = re.sub(r"\bLEN\s*\(", "CHAR_LENGTH(", sql, flags=re.IGNORECASE)

    # 8. Syspro archive table names ending in # (e.g. SorMaster#) break MySQL
    #    because '#' is a comment character. Backtick-escape the whole table name.
    #    e.g.  SorMaster#  →  `SorMaster#`
    #    Also strip any alias that follows to keep syntax clean.
    def _escape_hash_table(m: re.Match) -> str:
        name = m.group(1)  # e.g. SorMaster
        return f"`{name}#`"

    sql = re.sub(
        r"\b([A-Za-z][A-Za-z0-9_]*)#(?=[\s,;()]|$)",
        _escape_hash_table,
        sql,
    )

    return sql


# ── Column name extractor ──────────────────────────────────────────────────────

def extract_columns_from_sql(sql: str) -> list[str]:
    """Parse a SQL SELECT statement to extract column aliases or bare names.

    E.g. ``SELECT a.Col1 AS alias1, SUM(x) AS total FROM ...``
    returns ``["alias1", "total"]``.
    """
    match = re.search(
        r"^\s*select\s+(?:top\s+\S+\s+)?(.*)?\s+from\b",
        sql,
        re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return []

    select_clause = match.group(1)

    # Split by comma, respecting nested parentheses
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for char in select_clause:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())

    cols: list[str] = []
    for part in parts:
        as_match = re.search(r"\bAS\s+(\w+)\b", part, re.IGNORECASE)
        if as_match:
            cols.append(as_match.group(1))
        else:
            words = re.findall(r"\b\w+\b", part)
            if words:
                cols.append(words[-1])
    return cols
