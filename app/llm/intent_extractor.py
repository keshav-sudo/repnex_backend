"""Two-tier intent classification: classify → extract → (optional) converse."""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from pydantic import ValidationError

from app.core.exceptions import LLMError
from app.llm.client import get_llm, load_prompt
from app.schemas.query import IntentClassification, IntentResult


async def classify_intent(
    natural_language: str,
    *,
    context_window: list[dict[str, Any]] | None = None,
) -> IntentClassification:
    """Step 1: Is this conversational or executable?"""
    system = load_prompt("classify")
    user = json.dumps(
        {
            "question": natural_language,
            "context": (context_window or [])[-4:],
        },
        default=str,
    )
    raw = await get_llm().chat_json(system=system, user=user)
    try:
        return IntentClassification.model_validate(raw)
    except ValidationError as e:
        raise LLMError(f"Classification schema mismatch: {e.errors()}") from e


async def extract_intent(
    natural_language: str,
    *,
    template_candidates: list[dict[str, Any]],
    context_window: list[dict[str, Any]] | None = None,
) -> IntentResult:
    """Step 2: Map NL → template_id + params (with missing-param detection)."""
    system = load_prompt("intent")
    # Inject today's date so LLM can resolve relative phrases like 'last 6 months'
    today_str = date.today().isoformat()
    system = f"Today's date is {today_str}.\n\n{system}"
    user = json.dumps(
        {
            "question": natural_language,
            "context": (context_window or [])[-6:],
            "templates": template_candidates,
        },
        default=str,
    )
    raw = await get_llm().chat_json(system=system, user=user)
    try:
        return IntentResult.model_validate(raw)
    except ValidationError as e:
        raise LLMError(f"LLM intent did not match schema: {e.errors()}") from e


async def generate_conversational_response(
    natural_language: str,
    *,
    context_window: list[dict[str, Any]] | None = None,
) -> str:
    """Generate a helpful text response for conversational queries."""
    system = load_prompt("conversational")
    ctx_text = ""
    if context_window:
        recent = context_window[-4:]
        ctx_text = "\n\nRecent conversation:\n" + "\n".join(
            f"- {m.get('role', '?')}: {m.get('content', '')[:200]}" for m in recent
        )
    user = f"{natural_language}{ctx_text}"
    return await get_llm().chat_text(system=system, user=user, max_tokens=400)
