from __future__ import annotations

import os
from pathlib import Path
import yaml
from app.llm.client import get_llm
from app.core.logging import get_logger

log = get_logger(__name__)

# Look for v2 inside repnex_backend_complete, fall back to sibling
V2_DIR = Path(__file__).resolve().parents[2] / "v2"
if not V2_DIR.exists():
    V2_DIR = Path(__file__).resolve().parents[3] / "v2"

class SemanticResolver:
    """
    V2 Semantic Engine Resolver.
    Loads universal business ontologies and ERP-specific adapters to dynamically
    translate natural language queries into target-specific SQL queries.
    """

    def __init__(self, erp_type: str = "syspro"):
        self.erp_type = erp_type.lower().strip()
        self.ontology_dir = V2_DIR / "ontology"
        self.adapter_dir = V2_DIR / "adapters" / self.erp_type
        self.relationship_file = V2_DIR / "relationships" / self.erp_type / "joins.yaml"

    def load_ontology(self) -> dict[str, dict]:
        ontology = {}
        if not self.ontology_dir.exists():
            log.warning(f"Ontology directory not found: {self.ontology_dir}")
            return ontology
        
        for f in self.ontology_dir.glob("*.yaml"):
            try:
                with open(f, "r", encoding="utf-8") as file:
                    data = yaml.safe_load(file)
                    if data and "concept" in data:
                        ontology[data["concept"]] = data
            except Exception as e:
                log.error(f"Error loading ontology file {f}: {e}")
        return ontology

    def load_adapters(self) -> dict[str, dict]:
        adapters = {}
        if not self.adapter_dir.exists():
            log.warning(f"Adapter directory not found: {self.adapter_dir}")
            return adapters
        
        for f in self.adapter_dir.glob("*.yaml"):
            if f.name.startswith("_"):
                continue
            try:
                with open(f, "r", encoding="utf-8") as file:
                    data = yaml.safe_load(file)
                    if data and "concept" in data:
                        adapters[data["concept"]] = data
            except Exception as e:
                log.error(f"Error loading adapter file {f}: {e}")
        return adapters

    def load_joins(self) -> dict:
        if not self.relationship_file.exists():
            log.warning(f"Relationships joins file not found: {self.relationship_file}")
            return {}
        try:
            with open(self.relationship_file, "r", encoding="utf-8") as file:
                return yaml.safe_load(file) or {}
        except Exception as e:
            log.error(f"Error loading relationship file {self.relationship_file}: {e}")
            return {}

    def _load_meta(self) -> dict:
        """Load _meta.yaml for complete schema inventory."""
        meta_path = self.adapter_dir / "_meta.yaml"
        if not meta_path.exists():
            return {}
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            log.error(f"Error loading meta file {meta_path}: {e}")
            return {}

    def _get_dialect(self) -> str:
        """Returns the SQL dialect for this ERP adapter: 'mysql', 'postgres', or 'mssql'."""
        meta = self._load_meta()
        return meta.get("conventions", {}).get("dialect") or meta.get("dialect") or (
            "postgres" if self.erp_type == "helios" else "mssql"
        )

    def build_prompt_context(self) -> str:
        ontology = self.load_ontology()
        adapters = self.load_adapters()
        joins = self.load_joins()
        meta = self._load_meta()

        context = []
        context.append(f"ERP Type: {self.erp_type.upper()}\n")

        # ── Schema inventory from _meta.yaml ─────────────────────────
        tables_meta = meta.get("tables") or {}
        if tables_meta:
            context.append("--- COMPLETE DATABASE SCHEMA (ONLY these tables/columns exist) ---")
            for tbl_name, tbl_info in tables_meta.items():
                alias = tbl_info.get("alias", "")
                cols = ", ".join(tbl_info.get("columns", []))
                context.append(f"  Table: {tbl_name} (alias: {alias})  Columns: [{cols}]")
                if tbl_info.get("notes"):
                    context.append(f"    Notes: {tbl_info['notes']}")
            context.append("")

        # ── Data rules from _meta.yaml ───────────────────────────────
        data_rules = meta.get("data_rules") or {}
        if data_rules:
            context.append("--- DATA RULES ---")
            for rule_name, rule_desc in data_rules.items():
                context.append(f"  {rule_name}: {str(rule_desc).strip()}")
            context.append("")

        # ── Business concepts & mapping ──────────────────────────────
        context.append("--- BUSINESS CONCEPTS & FIELD MAPPINGS ---")

        for concept_name, ont in ontology.items():
            adapter = adapters.get(concept_name)
            if not adapter:
                continue

            context.append(f"\nConcept: {concept_name} (Module: {ont.get('module')})")
            context.append(f"  Description: {ont.get('description', '').strip()}")
            context.append(f"  Synonyms: {', '.join(ont.get('synonyms', []))}")

            # Primary table
            header_table = adapter.get("header_table") or adapter.get("table")
            alias = adapter.get("alias", "")
            detail_table = adapter.get("detail_table")

            if header_table:
                context.append(f"  Primary Table: {header_table} (alias: {alias})")
            if detail_table:
                detail_alias = adapter.get("detail_alias", "")
                detail_join = adapter.get("detail_join", "")
                context.append(f"  Detail Table: {detail_table} (alias: {detail_alias}, join: {detail_join})")

            # Header fields
            context.append("  Fields:")
            fields = adapter.get("fields") or adapter.get("header_fields") or {}
            for u_field, db_col in fields.items():
                context.append(f"    - {u_field}: {db_col}")

            # Calculated fields
            calc_fields = adapter.get("calculated_fields") or {}
            for u_field, expr in calc_fields.items():
                context.append(f"    - {u_field} (calculated): {expr}")

            # Detail fields
            detail_fields = adapter.get("detail_fields") or {}
            if detail_fields:
                context.append("  Detail Line Fields:")
                for u_field, db_col in detail_fields.items():
                    context.append(f"    - {u_field}: {db_col}")

            # Filters
            filters = adapter.get("filters") or adapter.get("default_filters") or {}
            if filters:
                context.append("  Predefined Filters:")
                for fname, fexpr in filters.items():
                    context.append(f"    - {fname}: {fexpr}")

            # Adapter-level joins (table-specific)
            adapter_joins = adapter.get("joins") or {}
            if adapter_joins:
                context.append("  Available Joins:")
                for jname, jinfo in adapter_joins.items():
                    context.append(f"    - {jname}: {jinfo.get('table')} {jinfo.get('alias', '')} ON {jinfo.get('on', '')}")

            # Balance / Additional tables
            bal_map = adapter.get("balance_mapping")
            if bal_map:
                context.append(f"  Balance Table: {bal_map.get('table')} (Join: {bal_map.get('join_on')})")
                for u_field, db_col in bal_map.get("fields", {}).items():
                    context.append(f"    - {u_field}: {db_col}")

            # Sample SQL
            sample_sql = adapter.get("sample_sql")
            if sample_sql and isinstance(sample_sql, dict):
                context.append("  Reference SQL Examples:")
                for sq_name, sq_val in sample_sql.items():
                    context.append(f"    [{sq_name}]: {str(sq_val).strip()}")
            elif sample_sql and isinstance(sample_sql, str):
                context.append(f"  Reference SQL: {sample_sql.strip()}")

        # ── Global Join Relationships ────────────────────────────────
        context.append("\n--- JOIN RELATIONSHIPS (joins.yaml) ---")
        relationships = joins.get("relationships", [])
        for rel in relationships:
            context.append(f"  - {rel.get('join_type', 'LEFT')} JOIN: {rel.get('from_concept')} -> {rel.get('to_concept')} ON {rel.get('condition')}")

        return "\n".join(context)

    async def translate_to_sql(
        self,
        natural_language: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """
        Translates NL query into valid SQL dialect of the ERP target.
        """
        prompt_context = self.build_prompt_context()

        # Build dialect-specific instructions
        dialect = self._get_dialect()

        if dialect == "postgres":
            dialect_instructions = """5. All queries should target PostgreSQL / Supabase dialect:
   - Use 'CURRENT_DATE' for the current date.
   - For Year-To-Date (YTD) filtering, use: `[date_column] >= DATE_TRUNC('year', CURRENT_DATE) AND [date_column] <= CURRENT_DATE`.
   - Limit rows using: `LIMIT N` at the end of the query (do NOT use 'SELECT TOP N').
   - For Margin/Profitability: compute from hx_sales_invoice_line (unit_price) vs hx_item (std_cost). Do NOT fabricate cost/profit columns on hx_sales_invoice.
   - Use ROUND(..., 2) and CAST(... AS NUMERIC) for safe division. Always wrap denominators in NULLIF(..., 0)."""
        elif dialect == "mysql":
            dialect_instructions = """5. All queries MUST use MySQL dialect (target is Railway MySQL):
   - Use 'NOW()' for the current datetime and 'CURDATE()' for the current date.
   - For Year-To-Date (YTD) filtering, use: `[date_column] >= DATE_FORMAT(NOW(), '%Y-01-01') AND [date_column] <= NOW()`.
   - Limit rows using: `SELECT ... FROM ... LIMIT N` at the END of the query.
     DO NOT use 'SELECT TOP N' — that is T-SQL and will cause a syntax error in MySQL.
   - For safe division: `CAST([numerator] AS DECIMAL(18,4)) / NULLIF(CAST([denominator] AS DECIMAL(18,4)), 0)`.
   - String literals use single quotes. Column/table names need NO quoting unless they are reserved words.
   - Date comparisons: use `[date_col] >= '2024-01-01'` format (ISO-8601).
   - NEVER use square brackets [ ] around column or table names — that is T-SQL syntax.
   - NEVER use GETDATE(), DATEADD(), DATEDIFF(), DATEFROMPARTS() — those are T-SQL functions.
   - Use MySQL equivalents: NOW(), DATE_ADD(), DATEDIFF(), STR_TO_DATE()."""
        else:
            # Legacy MSSQL fallback (should not be hit for current adapters)
            dialect_instructions = """5. All queries should target MS SQL Server / T-SQL dialect:
   - Use 'GETDATE()' for the current date.
   - For Year-To-Date (YTD) filtering, use: `[date_column] >= DATEFROMPARTS(YEAR(GETDATE()), 1, 1) AND [date_column] <= GETDATE()`.
   - Limit rows using: `SELECT TOP N ...` at the start of the query (do NOT use LIMIT).
   - For Margin or Profitability queries: `(CAST([ytd_profit] AS decimal(18,4)) / NULLIF(CAST([ytd_sales] AS decimal(18,4)), 0))`."""

        system_prompt = f"""You are a precise, deterministic NL-to-SQL translator for an ERP database.
Your job is to translate a user's natural language question into a single valid SQL query.

{prompt_context}

CRITICAL RULES:
1. Use ONLY the tables and columns listed in 'COMPLETE DATABASE SCHEMA' above. NO exceptions.
2. Do NOT guess, hallucinate, or invent table names, column names, or aliases.
3. If joining tables, use the exact join conditions defined in 'Available Joins' or 'JOIN RELATIONSHIPS'.
4. Do NOT output markdown code blocks. Output ONLY raw SQL.
{dialect_instructions}
6. OUT-OF-SCHEMA HANDLING: If the user asks for data that CANNOT be answered from the available schema
   (e.g., columns that don't exist, modules not present like HR/Payroll/CRM, or concepts not mapped),
   respond with EXACTLY this prefix: CONVERSATIONAL: followed by a helpful explanation of what data
   IS available and what the user can ask instead. Do NOT generate invalid SQL.
7. When using Reference SQL Examples from the adapter context, adapt them to the user's specific question
   but preserve the join logic and column references exactly.
"""

        if start_date and end_date:
            if dialect == "postgres":
                system_prompt += f"""
6. CRITICAL: The user has specified a custom date range: from '{start_date}' to '{end_date}'. 
   If the query filters by any date fields (such as invoice date, due date, payment date, transaction date, journal date, etc.), 
   you MUST filter them using: `[date_field] >= '{start_date}' AND [date_field] <= '{end_date}'`. 
   Do NOT use CURRENT_DATE or DATE_TRUNC() in this case. Write the conditions using these exact literal values.
"""
            else:
                system_prompt += f"""
8. CRITICAL: The user has specified a custom date range: from '{start_date}' to '{end_date}'. 
   If the query filters by any date fields (such as invoice date, due date, payment date, transaction date, journal date, etc.), 
   you MUST filter them using: `[date_field] >= '{start_date}' AND [date_field] <= '{end_date}'`. 
   Do NOT use NOW(), CURDATE(), GETDATE() or any dynamic date functions in this case. Write the conditions using these exact literal values.
"""

        log.info(f"Generating V2 semantic query for: {natural_language}")
        
        # Use get_llm() which handles fallback to OpenAI and tenacity retries automatically
        sql = await get_llm().chat_text(
            system=system_prompt,
            user=f"Translate this query: {natural_language}",
            max_tokens=1024
        )
        
        # Clean up and extract SQL from markdown code blocks if present
        import re
        cleaned_sql = sql.strip()
        
        # 1. Closed code blocks
        match = re.search(r"```sql\s*(.*?)\s*```", cleaned_sql, re.IGNORECASE | re.DOTALL)
        if match:
            cleaned_sql = match.group(1).strip()
        else:
            match = re.search(r"```\s*(.*?)\s*```", cleaned_sql, re.DOTALL)
            if match:
                cleaned_sql = match.group(1).strip()
            else:
                # 2. Unclosed code blocks (e.g. if truncated by token limit)
                match = re.search(r"```sql\s*(.*)", cleaned_sql, re.IGNORECASE | re.DOTALL)
                if match:
                    cleaned_sql = match.group(1).strip()
                else:
                    match = re.search(r"```\s*(.*)", cleaned_sql, re.DOTALL)
                    if match:
                        cleaned_sql = match.group(1).strip()
                    else:
                        # Clean up any partial wrapping backticks
                        if cleaned_sql.startswith("```sql"):
                            cleaned_sql = cleaned_sql[6:]
                        if cleaned_sql.startswith("```"):
                            cleaned_sql = cleaned_sql[3:]
                        if cleaned_sql.endswith("```"):
                            cleaned_sql = cleaned_sql[:-3]
                            
        cleaned_sql = cleaned_sql.strip()

        # ── MySQL safety post-processor ───────────────────────────────────────
        # If the dialect is MySQL, auto-fix any T-SQL that the LLM still produced.
        if dialect == "mysql":
            cleaned_sql = _fix_tsql_to_mysql(cleaned_sql)

        # Check if the extracted text contains standard SQL queries (SELECT or WITH)
        sql_upper = cleaned_sql.upper()
        if "SELECT" not in sql_upper and "WITH" not in sql_upper:
            # Conversational/clarification text
            return f"CONVERSATIONAL:{sql}"

        return cleaned_sql


def _fix_tsql_to_mysql(sql: str) -> str:
    """
    Safety post-processor: converts T-SQL patterns to MySQL equivalents.
    This is a defense-in-depth layer — the LLM prompt should already produce
    correct MySQL, but this catches edge cases.
    """
    import re

    # 1. SELECT TOP N ... → SELECT ... LIMIT N
    #    Matches: SELECT TOP 10, SELECT TOP(10), SELECT DISTINCT TOP 10
    top_match = re.match(
        r"^\s*(SELECT\s+(?:DISTINCT\s+)?)(TOP\s*\(?\s*(\d+)\s*\)?)\s+",
        sql,
        re.IGNORECASE,
    )
    if top_match:
        limit_n = top_match.group(3)
        # Remove the TOP clause from the start
        sql = re.sub(
            r"^(\s*SELECT\s+(?:DISTINCT\s+)?)TOP\s*\(?\s*\d+\s*\)?\s+",
            r"\1",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )
        # Append LIMIT at the very end (before any trailing whitespace/semicolon)
        sql = re.sub(r"(;?\s*)$", f" LIMIT {limit_n}\\1", sql.rstrip(), count=1)

    # 2. GETDATE() → NOW()
    sql = re.sub(r"\bGETDATE\s*\(\s*\)", "NOW()", sql, flags=re.IGNORECASE)

    # 3. DATEFROMPARTS(YEAR(GETDATE()), 1, 1) → DATE_FORMAT(NOW(), '%Y-01-01')
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
        r"\bDATEADD\s*\(\s*(\w+)\s*,\s*(-?\d+)\s*,\s*([^)]+)\)",
        _dateadd_to_mysql,
        sql,
        flags=re.IGNORECASE,
    )

    # 5. Square bracket identifiers [ColumnName] → ColumnName (no quoting needed)
    sql = re.sub(r"\[([^\]]+)\]", r"\1", sql)

    # 6. ISNULL(a, b) → IFNULL(a, b)
    sql = re.sub(r"\bISNULL\s*\(", "IFNULL(", sql, flags=re.IGNORECASE)

    # 7. LEN(x) → CHAR_LENGTH(x)
    sql = re.sub(r"\bLEN\s*\(", "CHAR_LENGTH(", sql, flags=re.IGNORECASE)

    return sql


def extract_columns_from_sql(sql: str) -> list[str]:
    """
    Parses a SQL SELECT statement to extract column aliases or names.
    E.g. SELECT a.Col1 AS alias1, Col2 AS alias2, col3 FROM ...
    """
    import re
    # Find the SELECT part (everything between SELECT and FROM)
    # Handle case insensitivity and multi-line SQL
    match = re.search(r"^\s*select\s+(?:top\s+\S+\s+)?(.*)\s+from\b", sql, re.IGNORECASE | re.DOTALL)
    if not match:
        return []
    
    select_clause = match.group(1)
    
    # Split by commas, taking care of parentheses (like COUNT(*), SUM(x), etc.)
    parts = []
    current = []
    depth = 0
    for char in select_clause:
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
        if char == ',' and depth == 0:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    if current:
        parts.append("".join(current).strip())
        
    cols = []
    for part in parts:
        # Match "expression AS alias" (case insensitive)
        as_match = re.search(r"\bAS\s+(\w+)\b", part, re.IGNORECASE)
        if as_match:
            cols.append(as_match.group(1))
        else:
            # Fallback: get the last word (e.g. "a.ColumnName" -> "ColumnName")
            words = re.findall(r"\b\w+\b", part)
            if words:
                cols.append(words[-1])
    return cols

