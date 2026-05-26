from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import LLMError
from app.llm.client import get_llm, load_prompt
from app.schemas.query import IntentResult


async def extract_intent(
    natural_language: str,
    *,
    templates_catalog: list[dict[str, Any]],
    context_window: list[dict[str, Any]] | None = None,
) -> IntentResult:
    """Map NL → IntentResult. Never produces SQL."""
    system = load_prompt("intent")
    user = json.dumps(
        {
            "question": natural_language,
            "context": (context_window or [])[-6:],
            "templates": templates_catalog,
        },
        default=str,
    )
    raw = await get_llm().chat_json(system=system, user=user)
    try:
        return IntentResult.model_validate(raw)
    except ValidationError as e:
        raise LLMError(f"LLM intent did not match schema: {e.errors()}") from e
