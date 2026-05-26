from __future__ import annotations

import json
from typing import Any

from app.llm.client import get_llm, load_prompt


async def generate_insight(
    *, intent: dict[str, Any], rows: list[dict[str, Any]], sample: int = 50
) -> str:
    system = load_prompt("insight")
    user = json.dumps({"intent": intent, "sample_rows": rows[:sample]}, default=str)
    return await get_llm().chat_text(system=system, user=user, max_tokens=120)
