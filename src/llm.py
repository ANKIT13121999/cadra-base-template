"""Minimal OpenAI-compatible chat client for the Cadra proxy.

No secrets are logged. Configuration comes from environment variables first,
then from ``opencode.json`` for local development.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import httpx


class LLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ModelCallResult:
    content: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int

    def as_model_call(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }


def _opencode_config() -> dict[str, Any]:
    path = Path(__file__).resolve().parents[1] / "opencode.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "cadra",
        timeout_seconds: float = 90.0,
    ) -> None:
        config = _opencode_config()
        options = config.get("provider", {}).get("cadra", {}).get("options", {}) if isinstance(config, dict) else {}
        self.base_url = (
            base_url
            or os.getenv("LLM_BASE_URL")
            or os.getenv("CADRA_PROXY_URL")
            or os.getenv("CADRA_BASE_URL")
            or os.getenv("OPENAI_BASE_URL")
            or str(options.get("baseURL") or "")
        ).rstrip("/")
        self.api_key = (
            api_key
            or os.getenv("LLM_API_KEY")
            or os.getenv("CADRA_TOKEN")
            or os.getenv("CADRA_API_KEY")
            or os.getenv("OPENAI_API_KEY")
            or str(options.get("apiKey") or "")
        )
        self.model = os.getenv("MODEL_NAME") or os.getenv("CADRA_MODEL") or os.getenv("CADRA_MODEL_ID") or model
        self.timeout_seconds = float(os.getenv("CADRA_TIMEOUT_SECONDS", timeout_seconds))
        self.input_price_per_mtok = float(os.getenv("PRICE_IN_PER_MTOK", "0.60"))
        self.output_price_per_mtok = float(os.getenv("PRICE_OUT_PER_MTOK", "3.00"))

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def chat_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _calculate_cost(self, usage: dict[str, Any], input_tokens: int, output_tokens: int) -> float:
        return round(
            (input_tokens / 1_000_000) * self.input_price_per_mtok
            + (output_tokens / 1_000_000) * self.output_price_per_mtok,
            8,
        )

    def chat(
        self,
        messages: Iterable[dict[str, str]],
        *,
        max_tokens: int = 800,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> ModelCallResult:
        if not self.configured:
            raise LLMError("LLM is not configured (set CADRA_PROXY_URL and CADRA_TOKEN)")
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "reasoning_effort": "none",
            "reasoning": {"enabled": False},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        started = time.perf_counter()
        try:
            response = httpx.post(
                self.chat_url,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc
        latency_ms = int(round((time.perf_counter() - started) * 1000))
        if response.status_code >= 400:
            raise LLMError(f"LLM returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise LLMError("LLM returned non-JSON HTTP body") from exc
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("LLM returned no choices")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM returned empty content")
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return ModelCallResult(
            content=content.strip(),
            model=str(data.get("model") or self.model),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=self._calculate_cost(usage, input_tokens, output_tokens),
            latency_ms=latency_ms,
        )


def extract_json_object(text: str) -> dict[str, Any] | None:
    cleaned = text.strip()
    cleaned = re.sub(r"<think\b[^>]*>.*?</think\s*>", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if "<think" in cleaned.lower():
        cleaned = cleaned[: cleaned.lower().find("<think")].strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].lstrip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
