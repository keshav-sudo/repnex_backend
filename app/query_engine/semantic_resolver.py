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
        if self.erp_type == "helios":
            dialect_instructions = """5. All queries should target PostgreSQL / Supabase dialect:
   - Use 'CURRENT_DATE' for the current date.
   - For Year-To-Date (YTD) filtering, use: `[date_column] >= DATE_TRUNC('year', CURRENT_DATE) AND [date_column] <= CURRENT_DATE`.
   - Limit rows using: `LIMIT N` at the end of the query (do NOT use 'SELECT TOP N').
   - For Margin/Profitability: compute from hx_sales_invoice_line (unit_price) vs hx_item (std_cost). Do NOT fabricate cost/profit columns on hx_sales_invoice.
   - Use ROUND(..., 2) and CAST(... AS NUMERIC) for safe division. Always wrap denominators in NULLIF(..., 0)."""
        else:
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
            if self.erp_type == "helios":
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
   Do NOT use GETDATE() or DATEADD() or other dynamic date functions in this case. Write the conditions using these exact literal values.
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
        
        # Check if the extracted text contains standard SQL queries (SELECT or WITH)
        sql_upper = cleaned_sql.upper()
        if "SELECT" not in sql_upper and "WITH" not in sql_upper:
            # Conversational/clarification text
            return f"CONVERSATIONAL:{sql}"
            
        return cleaned_sql


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

