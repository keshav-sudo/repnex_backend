"""
LLM client — DeepSeek (primary) with OpenAI fallback.
Both providers are OpenAI-SDK compatible so we just swap the base_url + api_key.
"""
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


def _make_client(api_key: str, base_url: str | None, timeout: int) -> AsyncOpenAI:
    kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
    if base_url:
        kwargs["base_url"] = base_url
    return AsyncOpenAI(**kwargs)


class LLMClient:
    """
    Tries DeepSeek first (if DEEPSEEK_API_KEY is set), falls back to OpenAI.
    Both APIs are OpenAI-SDK compatible.
    """

    def __init__(self) -> None:
        s = get_settings()
        self.timeout = s.LLM_TIMEOUT_S
        self.max_retries = s.LLM_MAX_RETRIES

        # Determine which provider to use as primary
        ds_key = s.DEEPSEEK_API_KEY.strip()
        oa_key = s.OPENAI_API_KEY.strip()

        is_dummy_oa = not oa_key or oa_key in ("your-openai-api-key", "test", "dummy", "")
        is_valid_ds = bool(ds_key) and ds_key not in ("", "your-deepseek-api-key")

        if is_valid_ds:
            # DeepSeek primary
            self._primary_client = _make_client(ds_key, s.DEEPSEEK_BASE_URL, self.timeout)
            self._primary_model = s.DEEPSEEK_MODEL
            self._primary_name = "deepseek"

            if not is_dummy_oa:
                self._fallback_client = _make_client(oa_key, None, self.timeout)
                self._fallback_model = s.OPENAI_MODEL
                self._fallback_name = "openai"
            else:
                self._fallback_client = None
                self._fallback_model = None
                self._fallback_name = None
        elif not is_dummy_oa:
            # OpenAI only
            self._primary_client = _make_client(oa_key, None, self.timeout)
            self._primary_model = s.OPENAI_MODEL
            self._primary_name = "openai"
            self._fallback_client = None
            self._fallback_model = None
            self._fallback_name = None
        else:
            # No real key — simulation mode
            self._primary_client = None
            self._primary_model = None
            self._primary_name = "simulation"
            self._fallback_client = None
            self._fallback_model = None
            self._fallback_name = None

        log.info(
            "llm_client_init",
            extra={
                "primary": self._primary_name,
                "fallback": self._fallback_name or "none",
            },
        )

    @property
    def _is_simulation(self) -> bool:
        return self._primary_client is None

    # ── internal call helper ────────────────────────────────────────────

    async def _call_json(
        self,
        client: AsyncOpenAI,
        model: str,
        system: str,
        user: str,
    ) -> dict[str, Any]:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=0.5, max=4.0),
            retry=retry_if_exception_type((APIError, RateLimitError, TimeoutError)),
        )
        async def _inner() -> dict[str, Any]:
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
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
            return await _inner()
        except (APIError, RateLimitError) as e:
            raise LLMError(f"LLM error ({model}): {e.__class__.__name__}") from e

    async def _call_text(
        self,
        client: AsyncOpenAI,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 256,
    ) -> str:
        @retry(
            reraise=True,
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential_jitter(initial=0.5, max=4.0),
            retry=retry_if_exception_type((APIError, RateLimitError, TimeoutError)),
        )
        async def _inner() -> str:
            try:
                resp = await client.chat.completions.create(
                    model=model,
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
            return await _inner()
        except (APIError, RateLimitError) as e:
            raise LLMError(f"LLM error ({model}): {e.__class__.__name__}") from e

    # ── public API ──────────────────────────────────────────────────────

    async def chat_json(
        self, *, system: str, user: str, schema_hint: str | None = None
    ) -> dict[str, Any]:
        prompt_user = user if not schema_hint else f"{user}\n\n{schema_hint}"

        if self._is_simulation:
            return self._simulate_json(system=system, user=user)

        # Try primary
        try:
            return await self._call_json(self._primary_client, self._primary_model, system, prompt_user)  # type: ignore[arg-type]
        except LLMError as primary_err:
            log.warning(
                "llm_primary_failed",
                extra={"provider": self._primary_name, "err": str(primary_err)},
            )
            if self._fallback_client and self._fallback_model:
                log.info("llm_fallback_attempt", extra={"provider": self._fallback_name})
                return await self._call_json(self._fallback_client, self._fallback_model, system, prompt_user)
            raise

    async def chat_text(self, *, system: str, user: str, max_tokens: int = 256) -> str:
        if self._is_simulation:
            return "Hello! I am the Repnex AI assistant. How can I help you query your ERP data today?"

        try:
            return await self._call_text(self._primary_client, self._primary_model, system, user, max_tokens)  # type: ignore[arg-type]
        except LLMError as primary_err:
            log.warning(
                "llm_primary_failed",
                extra={"provider": self._primary_name, "err": str(primary_err)},
            )
            if self._fallback_client and self._fallback_model:
                return await self._call_text(self._fallback_client, self._fallback_model, system, user, max_tokens)
            raise

    # ── simulation mode (no API key available) ─────────────────────────

    def _simulate_json(self, *, system: str, user: str) -> dict[str, Any]:
        """Lightweight heuristic simulation when no LLM key is available."""
        try:
            user_data = json.loads(user)
        except Exception:
            user_data = {}

        question = user_data.get("question", "").lower()

        # Classify intent
        if "classify" in system.lower() or ("question" in user_data and "templates" not in user_data):
            conversational_kw = ["hello", "hi", "hey", "who are you", "what can you do", "help", "thanks", "thank you"]
            is_conv = any(kw in question for kw in conversational_kw)
            return {
                "type": "conversational" if is_conv else "executable",
                "confidence": 1.0,
                "reasoning": "Simulated intent classification (no LLM key)",
            }

        # Extract intent from template candidates
        if "intent" in system.lower() or "templates" in user_data:
            templates = user_data.get("templates", [])
            if not templates:
                return {
                    "template_id": None,
                    "params": {},
                    "missing_params": [],
                    "confidence": 0.0,
                    "rationale": "No template candidates available",
                }

            matched = templates[0]
            template_id = matched.get("id")
            extracted_params: dict[str, Any] = {}
            template_params = matched.get("params", {})

            import re
            if "limit" in template_params:
                m = re.search(r"\b(limit|top)\s+(\d+)\b", question)
                extracted_params["limit"] = int(m.group(2)) if m else 10

            date_phrases = ["last 6 months", "last 3 months", "last month", "last quarter", "last year", "this year"]
            phrase = next((p for p in date_phrases if p in question), None)
            if "start_date" in template_params:
                extracted_params["start_date"] = phrase or "last month"
            elif "period" in template_params:
                extracted_params["period"] = (phrase or "last month").replace(" ", "_")

            return {
                "template_id": template_id,
                "params": extracted_params,
                "missing_params": [],
                "confidence": 0.95,
                "rationale": f"Simulated match to {template_id}",
            }

        return {"type": "conversational", "confidence": 1.0, "reasoning": "Default fallback"}


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
