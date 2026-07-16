"""Semantic Resolver — translates natural language queries into dialect-correct SQL.

Pipeline per call:
  1. ContextBuilder assembles the YAML knowledge graph into a prompt context string.
  2. DialectBuilder detects the SQL dialect and generates dialect-specific rules.
  3. LLM generates raw SQL from the system prompt + user query.
  4. SQLCleaner strips markdown fences and applies T-SQL→MySQL post-processing.
"""
from __future__ import annotations

from app.core.logging import get_logger
from app.engine.resolver.context_builder import ContextBuilder
from app.engine.resolver.dialect_builder import (
    build_date_range_instructions,
    build_dialect_instructions,
    get_dialect,
)
from app.engine.resolver.sql_cleaner import clean_llm_sql, fix_tsql_to_mysql
from app.llm.client import get_llm

log = get_logger(__name__)

_SYSTEM_PROMPT_TEMPLATE = """\
You are a precise, deterministic NL-to-SQL translator for an ERP database.
Your job is to translate a user's natural language question into a single valid SQL query.

{context}

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
7. When using Reference SQL Examples from the adapter context, adapt them to the user's specific
   question but preserve the join logic and column references exactly.
8. ARITHMETIC & NEGATIVE VALUES: Pay close attention to negative and positive values. When calculating margins, balances, or outstanding values, ensure you use the correct mathematical sign (+ or -) and logic. Outstanding balance filters must check for non-zero or positive/negative amounts as appropriate (e.g. outstanding invoices typically have a positive remaining balance, while negative amounts might indicate credit notes or pre-payments depending on the field meaning).
"""


class SemanticResolver:
    """NL-to-SQL translator using the V2 YAML-ontology semantic engine.

    Attributes:
        erp_type: Normalised ERP identifier (e.g. 'syspro', 'helios', 'epicor').
    """

    def __init__(self, erp_type: str = "syspro", target_dialect: str | None = None, *args, **kwargs) -> None:
        self.erp_type = erp_type.lower().strip()
        if target_dialect:
            td = target_dialect.lower().strip()
            self.target_dialect = "postgres" if td == "cloudsql" else td
        else:
            self.target_dialect = None
        self._context_builder = ContextBuilder(self.erp_type)

    async def translate_to_sql(
        self,
        natural_language: str,
        start_date: str | None = None,
        end_date: str | None = None,
        history: list[dict] | None = None,
    ) -> str:
        """Translate a natural language query into a valid SQL string.

        Returns:
            A raw SQL string, or a string prefixed with ``CONVERSATIONAL:``
            when the query cannot be answered from the available schema.
        """
        import asyncio
        import inspect
        meta_res = self._context_builder.load_meta()
        if inspect.iscoroutine(meta_res) or asyncio.iscoroutine(meta_res):
            meta = await meta_res
        else:
            meta = meta_res
        dialect = self.target_dialect or get_dialect(self.erp_type, meta)
        context = await self._context_builder.build(natural_language)

        system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            context=context,
            dialect_instructions=build_dialect_instructions(dialect),
        )

        if start_date and end_date:
            system_prompt += "\n" + build_date_range_instructions(dialect, start_date, end_date)

        log.info(
            "semantic_translate",
            extra={"erp": self.erp_type, "dialect": dialect, "nl": natural_language[:120]},
        )

        history_str = ""
        if history:
            recent_history = history[-5:]
            history_str = "\n\nCONVERSATION HISTORY (Use this context to resolve follow-ups, pronouns like 'this', 'that', 'it', 'them', or references to previous results):\n"
            for turn in recent_history:
                role = turn.get("role", "user")
                content = turn.get("content", "")
                sql_info = ""
                if "sql" in turn and turn["sql"]:
                    sql_info = f" [SQL generated: {turn['sql']}]"
                history_str += f"- {role.upper()}: {content}{sql_info}\n"

        user_prompt = f"Translate this query: {natural_language}"
        if history_str:
            user_prompt = f"{history_str}\nNew Query (contextual follow-up): {natural_language}\nTranslate this new query considering the conversation history and previous SQL queries."

        raw_sql = await get_llm().chat_text(
            system=system_prompt,
            user=user_prompt,
            max_tokens=1024,
        )

        sql = clean_llm_sql(raw_sql)

        if dialect == "mysql":
            sql = fix_tsql_to_mysql(sql)

        # Route non-SQL responses (clarifications / out-of-schema) back as CONVERSATIONAL
        sql_upper = sql.upper()
        if "SELECT" not in sql_upper and "WITH" not in sql_upper:
            return f"CONVERSATIONAL:{raw_sql}"

        return sql
