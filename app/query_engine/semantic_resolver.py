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

    def build_prompt_context(self) -> str:
        ontology = self.load_ontology()
        adapters = self.load_adapters()
        joins = self.load_joins()

        context = []
        context.append(f"ERP Type: {self.erp_type.upper()}\n")
        context.append("--- UNIVERSAL BUSINESS CONCEPTS & MAPPING RULES ---")

        for concept_name, ont in ontology.items():
            adapter = adapters.get(concept_name)
            if not adapter:
                continue

            context.append(f"\nConcept: {concept_name} (Module: {ont.get('module')})")
            context.append(f"  Description: {ont.get('description', '').strip()}")
            context.append(f"  Synonyms: {', '.join(ont.get('synonyms', []))}")

            # Map Table names
            header_table = adapter.get("header_table") or adapter.get("table")
            detail_table = adapter.get("detail_table")

            if header_table:
                context.append(f"  Primary Database Table: {header_table}")
            if detail_table:
                context.append(f"  Secondary/Detail Database Table: {detail_table}")

            # Map Fields
            context.append("  Fields Mapping (Universal Concept Field -> ERP Database Column):")
            
            # Header fields
            fields = adapter.get("fields") or adapter.get("header_fields") or {}
            for u_field, db_col in fields.items():
                context.append(f"    - {u_field}: {db_col}")

            # Detail fields
            detail_fields = adapter.get("detail_fields") or {}
            for u_field, db_col in detail_fields.items():
                context.append(f"    - line.{u_field}: {db_col}")

            # Balance / Additional tables
            bal_map = adapter.get("balance_mapping")
            if bal_map:
                context.append(f"    - Additional Joined Table: {bal_map.get('table')} (Join on: {bal_map.get('join_on')})")
                for u_field, db_col in bal_map.get("fields", {}).items():
                    context.append(f"      - {u_field}: {db_col}")

        # Add Joins context
        context.append("\n--- JOIN RELATIONSHIPS (joins.yaml) ---")
        relationships = joins.get("relationships", [])
        for rel in relationships:
            context.append(f"  - Join: {rel.get('from_concept')} to {rel.get('to_concept')} using: {rel.get('condition')}")

        return "\n".join(context)

    async def translate_to_sql(self, natural_language: str) -> str:
        """
        Translates NL query into valid SQL dialect of the ERP target.
        """
        prompt_context = self.build_prompt_context()

        system_prompt = f"""You are a precise, deterministic NL-to-SQL translator for an ERP database.
Your job is to translate a user's natural language question into a single valid SQL query.

{prompt_context}

CRITICAL RULES:
1. Use ONLY the tables, columns, and joins specified above.
2. Do NOT guess or hallucinate table names or column names.
3. If joining tables, use the exact join conditions defined in 'JOIN RELATIONSHIPS' or 'Additional Joined Table'.
4. Do NOT output markdown code blocks. Output ONLY raw SQL.
5. All queries should target MS SQL Server / T-SQL dialect (unless specified otherwise).
"""

        log.info(f"Generating V2 semantic query for: {natural_language}")
        
        # Use get_llm() which handles fallback to OpenAI and tenacity retries automatically
        sql = await get_llm().chat_text(
            system=system_prompt,
            user=f"Translate this query: {natural_language}",
            max_tokens=1024
        )
        
        # Clean up any trailing backticks or formatting
        sql = sql.strip()
        if sql.startswith("```sql"):
            sql = sql[6:]
        if sql.startswith("```"):
            sql = sql[3:]
        if sql.endswith("```"):
            sql = sql[:-3]
        return sql.strip()


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

