from __future__ import annotations

from app.llm.client import get_llm, load_prompt


async def generate_title(natural_language: str) -> str:
    system = load_prompt("title")
    out = await get_llm().chat_text(system=system, user=natural_language, max_tokens=24)
    out = out.strip().strip('"').strip("'")
    return out[:80] or "New chat"
