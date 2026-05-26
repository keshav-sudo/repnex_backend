from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from openai import APIError, AsyncOpenAI, RateLimitError
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.config import get_settings
from app.core.exceptions import LLMError, LLMTimeout
from app.core.logging import get_logger

log = get_logger(__name__)

PROMPTS_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


class LLMClient:
    def __init__(self) -> None:
        s = get_settings()
        self.model = s.OPENAI_MODEL
        self.timeout = s.LLM_TIMEOUT_S
        self.max_retries = s.LLM_MAX_RETRIES
        self._client = AsyncOpenAI(api_key=s.OPENAI_API_KEY, timeout=self.timeout)

    async def chat_json(
        self, *, system: str, user: str, schema_hint: str | None = None
    ) -> dict[str, Any]:
        prompt_user = user if not schema_hint else f"{user}\n\n{schema_hint}"

        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=0.5, max=4.0),
            retry=retry_if_exception_type((APIError, RateLimitError, TimeoutError)),
        )
        async def _call() -> dict[str, Any]:
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt_user},
                    ],
                    temperature=0.1,
                )
            except TimeoutError as e:
                raise LLMTimeout("LLM timeout") from e
            text = resp.choices[0].message.content or "{}"
            try:
                return json.loads(text)
            except json.JSONDecodeError as e:
                raise LLMError(f"LLM returned invalid JSON: {e}") from e

        try:
            return await _call()
        except (APIError, RateLimitError) as e:
            raise LLMError(f"LLM error: {e.__class__.__name__}") from e

    async def chat_text(self, *, system: str, user: str, max_tokens: int = 256) -> str:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=0.5, max=4.0),
            retry=retry_if_exception_type((APIError, RateLimitError, TimeoutError)),
        )
        async def _call() -> str:
            try:
                resp = await self._client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.3,
                    max_tokens=max_tokens,
                )
            except TimeoutError as e:
                raise LLMTimeout("LLM timeout") from e
            return (resp.choices[0].message.content or "").strip()

        try:
            return await _call()
        except (APIError, RateLimitError) as e:
            raise LLMError(f"LLM error: {e.__class__.__name__}") from e


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
