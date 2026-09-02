from __future__ import annotations

import json
import http.client
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from aegora_runtime.config import DeepSeekSettings
from aegora_runtime.logging import get_logger, log_event


LOGGER = get_logger("deepseek")


class DeepSeekError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class DeepSeekClient:
    def __init__(self, settings: DeepSeekSettings):
        if not settings.api_key:
            raise DeepSeekError("LLM_GATEWAY_API_KEY is required")
        self.settings = settings
        self._lock = threading.Lock()
        self._usage: dict[str, Any] = {"total_calls": 0, "failed_calls": 0, "by_model": {}}

    def usage_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._usage))

    def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]:
        content = self.chat_text(messages, model=model, temperature=temperature, json_mode=True)
        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DeepSeekError(f"LLM gateway returned non-JSON content: {content[:200]}") from exc
        if not isinstance(data, dict):
            raise DeepSeekError("LLM gateway JSON response must be an object")
        return data

    def chat_text(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        json_mode: bool = False,
    ) -> str:
        model_name = model or self.settings.chat_model
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": [{"role": item.role, "content": item.content} for item in messages],
            "thinking": {"type": "enabled" if self.settings.enable_thinking else "disabled"},
        }
        if not self.settings.enable_thinking:
            payload["temperature"] = temperature
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        request = urllib.request.Request(
            f"{self.settings.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.settings.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            self._record_usage(model_name, failed=True)
            self._log_call(model_name, started, status="error", error_type="HTTPError")
            detail = exc.read().decode("utf-8", errors="replace")
            raise DeepSeekError(f"LLM gateway HTTP {exc.code}: {detail[:500]}") from exc
        except TimeoutError as exc:
            self._record_usage(model_name, failed=True)
            self._log_call(model_name, started, status="error", error_type="TimeoutError")
            raise DeepSeekError(f"LLM gateway request timed out: {exc}") from exc
        except http.client.IncompleteRead as exc:
            self._record_usage(model_name, failed=True)
            self._log_call(model_name, started, status="error", error_type="IncompleteRead")
            raise DeepSeekError(f"LLM gateway response incomplete: {exc}") from exc
        except (urllib.error.URLError, OSError) as exc:
            self._record_usage(model_name, failed=True)
            self._log_call(model_name, started, status="error", error_type=type(exc).__name__)
            raise DeepSeekError(f"LLM gateway request failed: {exc}") from exc

        try:
            data = json.loads(body)
            usage = data.get("usage") if isinstance(data, dict) else None
            self._record_usage(model_name, usage)
            self._log_call(model_name, started, status="ok", usage=usage)
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            self._record_usage(model_name, failed=True)
            self._log_call(model_name, started, status="error", error_type=type(exc).__name__)
            raise DeepSeekError(f"Unexpected LLM gateway response: {body[:500]}") from exc

    def _record_usage(self, model: str, usage: dict[str, Any] | None = None, *, failed: bool = False) -> None:
        with self._lock:
            self._usage["total_calls"] += 1
            if failed:
                self._usage["failed_calls"] += 1
            item = self._usage["by_model"].setdefault(
                model,
                {
                    "calls": 0,
                    "failed_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            item["calls"] += 1
            if failed:
                item["failed_calls"] += 1
            if usage:
                item["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
                item["completion_tokens"] += int(usage.get("completion_tokens") or 0)
                item["total_tokens"] += int(usage.get("total_tokens") or 0)

    def _log_call(
        self,
        model: str,
        started: float,
        *,
        status: str,
        usage: dict[str, Any] | None = None,
        error_type: str | None = None,
    ) -> None:
        log_event(
            LOGGER,
            logging.INFO if status == "ok" else logging.WARNING,
            "llm_call",
            model=model,
            status=status,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 2),
            usage={
                "prompt_tokens": int((usage or {}).get("prompt_tokens") or 0),
                "completion_tokens": int((usage or {}).get("completion_tokens") or 0),
                "total_tokens": int((usage or {}).get("total_tokens") or 0),
            },
            error_type=error_type,
        )
