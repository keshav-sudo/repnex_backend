"""Dialect Builder — SQL dialect detection and dialect-specific instruction generation.

Supports: postgres, mysql, mssql (T-SQL)
"""
from __future__ import annotations


def get_dialect(erp_type: str, meta: dict) -> str:
    """Determine the SQL dialect from ERP adapter metadata.

    Falls back to ``postgres`` for Helios, ``mssql`` for all others.
    """
    dialect = (
        meta.get("conventions", {}).get("dialect")
        or meta.get("dialect")
    )
    if dialect:
        return dialect.lower()
    return "postgres" if erp_type == "helios" else "mssql"


def build_dialect_instructions(dialect: str) -> str:
    """Return the dialect-specific rules block to inject into the system prompt."""
    if dialect == "postgres":
        return (
            "5. All queries should target PostgreSQL / Supabase dialect:\n"
            "   - Use 'CURRENT_DATE' for the current date.\n"
            "   - YTD filtering: `[date_col] >= DATE_TRUNC('year', CURRENT_DATE) AND [date_col] <= CURRENT_DATE`.\n"
            "   - Limit rows: `LIMIT N` at the end (do NOT use 'SELECT TOP N').\n"
            "   - Margin/Profitability: compute from line-level tables. Do NOT fabricate cost/profit columns.\n"
            "   - Use ROUND(..., 2) and CAST(... AS NUMERIC). Always wrap denominators in NULLIF(..., 0)."
        )
    if dialect == "mysql":
        return (
            "5. All queries MUST use MySQL dialect (target is Railway MySQL):\n"
            "   - Use 'NOW()' for datetime and 'CURDATE()' for date.\n"
            "   - YTD filtering: `[date_col] >= DATE_FORMAT(NOW(), '%Y-01-01') AND [date_col] <= NOW()`.\n"
            "   - Limit rows: `SELECT ... FROM ... LIMIT N` at the END. DO NOT use 'SELECT TOP N'.\n"
            "   - Safe division: `CAST([num] AS DECIMAL(18,4)) / NULLIF(CAST([den] AS DECIMAL(18,4)), 0)`.\n"
            "   - String literals use single quotes. No square brackets around names.\n"
            "   - NEVER use GETDATE(), DATEADD(), DATEDIFF(), DATEFROMPARTS() — those are T-SQL.\n"
            "   - MySQL equivalents: NOW(), DATE_ADD(), DATEDIFF(), STR_TO_DATE()."
        )
    # Default: MSSQL / T-SQL
    return (
        "5. All queries should target MS SQL Server / T-SQL dialect:\n"
        "   - Use 'GETDATE()' for the current date.\n"
        "   - YTD filtering: `[date_col] >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1) AND [date_col] <= GETDATE()`.\n"
        "   - Limit rows: `SELECT TOP N ...` at the start (do NOT use LIMIT).\n"
        "   - Safe division: `(CAST([ytd_profit] AS decimal(18,4)) / NULLIF(CAST([ytd_sales] AS decimal(18,4)), 0))`."
    )


def build_date_range_instructions(dialect: str, start_date: str, end_date: str) -> str:
    """Return the date-range override rule to append to the system prompt."""
    base = (
        f"CRITICAL: The user specified a custom date range: '{start_date}' to '{end_date}'.\n"
        f"   Filter any date fields using: `[date_field] >= '{start_date}' AND [date_field] <= '{end_date}'`.\n"
        "   Do NOT use any dynamic date functions in this case."
    )
    rule_num = "6." if dialect == "postgres" else "8."
    return f"{rule_num} {base}"
