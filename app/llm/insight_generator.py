from __future__ import annotations

import json
from typing import Any

from app.llm.client import get_llm, load_prompt


async def generate_insight(
    *,
    intent: dict[str, Any],
    rows: list[dict[str, Any]],
    sample: int = 50,
    user_name: str | None = None,
) -> str:
    system = load_prompt("insight")
    if user_name:
        system = f"The user you are helping is named {user_name}.\n\n{system}"
    user = json.dumps({"intent": intent, "sample_rows": rows[:sample]}, default=str)
    return await get_llm().chat_text(system=system, user=user, max_tokens=1024)
