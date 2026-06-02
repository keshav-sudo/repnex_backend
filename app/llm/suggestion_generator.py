"""Generate contextual follow-up query suggestions."""
from __future__ import annotations

import json
from typing import Any

from app.core.exceptions import LLMError
from app.core.logging import get_logger
from app.llm.client import get_llm, load_prompt

log = get_logger(__name__)


async def generate_suggestions(
    *,
    template_id: str,
    module: str,
    category: str,
    description: str,
    user_name: str | None = None,
) -> list[str]:
    """Return 3-4 contextual follow-up query suggestions."""
    system = load_prompt("suggestions")
    if user_name:
        system = f"The user you are suggesting for is named {user_name}.\n\n{system}"
    user = json.dumps(
        {
            "template_id": template_id,
            "module": module,
            "category": category,
            "description": description,
        }
    )
    try:
        raw = await get_llm().chat_json(system=system, user=user)
        if isinstance(raw, list):
            return [str(s) for s in raw[:4]]
        if isinstance(raw, dict):
            for key in ("suggestions", "questions", "follow_ups"):
                if key in raw and isinstance(raw[key], list):
                    return [str(s) for s in raw[key][:4]]
        return []
    except (LLMError, Exception) as e:
        log.warning("suggestions_failed", extra={"err": str(e)})
        return _fallback_suggestions(module, category)


def _fallback_suggestions(module: str, category: str) -> list[str]:
    """Hardcoded fallback suggestions by module."""
    _defaults = {
        "ap": [
            "Show AP ageing report",
            "List overdue supplier invoices",
            "Top suppliers by outstanding amount",
        ],
        "ar": [
            "Show AR ageing report",
            "List overdue customer invoices",
            "Top customers by revenue",
        ],
        "inventory": [
            "Show stock on hand",
            "Slow moving inventory report",
            "Stock valuation summary",
        ],
        "so": [
            "Sales orders this month",
            "Backlog by customer",
            "Outstanding sales orders",
        ],
        "po": [
            "Outstanding purchase orders",
            "PO receipts this month",
            "Supplier delivery performance",
        ],
        "gl": [
            "Trial balance for current period",
            "GL journal entries today",
            "P&L summary this month",
        ],
    }
    return _defaults.get(module, [
        "Show a summary report",
        "Compare with last period",
        "Break down by category",
    ])
