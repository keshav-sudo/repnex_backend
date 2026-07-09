"""Two-tier intent classification: classify → extract → (optional) converse."""
from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.core.exceptions import LLMError
from app.llm.client import get_llm, load_prompt
from app.schemas.query import IntentClassification, IntentResult
from pydantic import ValidationError


def _inject_user_name(prompt: str, user_name: str | None) -> str:
    if user_name:
        return prompt.replace("{user_name}", user_name)
    return prompt.replace("{user_name}", "User")


async def classify_intent(
    natural_language: str,
    *,
    context_window: list[dict[str, Any]] | None = None,
    user_name: str | None = None,
) -> IntentClassification:
    """Step 1: Is this conversational or executable?"""
    system = _inject_user_name(load_prompt("classify"), user_name)
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
    user_name: str | None = None,
) -> IntentResult:
    """Step 2: Map NL → template_id + params (with missing-param detection)."""
    system = _inject_user_name(load_prompt("intent"), user_name)
    # Inject today's date so LLM can resolve relative phrases like 'last 6 months'
    today_str = date.today().isoformat()
    system = f"Today's date is {today_str}.\n\n{system}"

    # Clean templates to only include what LLM needs, preventing SQL noise/hallucination
    cleaned_candidates = []
    for c in template_candidates:
        cleaned_candidates.append({
            "id": c.get("id"),
            "description": c.get("description", ""),
            "module": c.get("module", ""),
            "category": c.get("category", ""),
            "params": c.get("params", {}),
        })

    user = json.dumps(
        {
            "question": natural_language,
            "context": (context_window or [])[-6:],
            "templates": cleaned_candidates,
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
    user_name: str | None = None,
    ai_tone: str = "friendly",
) -> str:
    """Generate a helpful text response for conversational queries."""
    system = _inject_user_name(load_prompt("conversational"), user_name)
    system = system.replace("{ai_tone}", ai_tone)
    ctx_text = ""
    if context_window:
        recent = context_window[-4:]
        ctx_text = "\n\nRecent conversation:\n" + "\n".join(
            f"- {m.get('role', '?')}: {m.get('content', '')[:200]}" for m in recent
        )
    user = f"{natural_language}{ctx_text}"
    return await get_llm().chat_text(system=system, user=user, max_tokens=400)
