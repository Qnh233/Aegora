from __future__ import annotations

import http.client
import json
from unittest.mock import patch

import pytest

from aegora_runtime.config import DeepSeekSettings
from aegora_runtime.deepseek import ChatMessage, DeepSeekClient, DeepSeekError


class FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_incomplete_read_is_wrapped_as_deepseek_error() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="https://deepseek.local",
            chat_model="deepseek-chat",
            fast_model="deepseek-chat",
            timeout_seconds=1,
        )
    )

    with patch("urllib.request.urlopen", side_effect=http.client.IncompleteRead(b"")):
        with pytest.raises(DeepSeekError, match="response incomplete"):
            client.chat_text([ChatMessage("user", "hi")])


def test_usage_snapshot_records_success_tokens() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="https://deepseek.local",
            chat_model="deepseek-chat",
            fast_model="deepseek-chat",
            timeout_seconds=1,
        )
    )
    response = FakeResponse(
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )

    with patch("urllib.request.urlopen", return_value=response):
        assert client.chat_text([ChatMessage("user", "hi")]) == "ok"

    usage = client.usage_snapshot()
    assert usage["total_calls"] == 1
    assert usage["by_model"]["deepseek-chat"]["total_tokens"] == 5


def test_chat_text_logs_latency_and_usage() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="https://deepseek.local",
            chat_model="deepseek-chat",
            fast_model="deepseek-chat",
            timeout_seconds=1,
        )
    )
    response = FakeResponse(
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )

    with patch("urllib.request.urlopen", return_value=response):
        with patch("aegora_runtime.deepseek.log_event") as log_event:
            assert client.chat_text([ChatMessage("user", "hi")]) == "ok"

    _, _, event = log_event.call_args.args[:3]
    fields = log_event.call_args.kwargs
    assert event == "llm_call"
    assert fields["status"] == "ok"
    assert fields["model"] == "deepseek-chat"
    assert fields["usage"]["total_tokens"] == 5
    assert isinstance(fields["elapsed_ms"], float)


def test_default_gateway_request_is_provider_neutral() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="https://gateway.llmgtw.io/v1",
            chat_model="deepseek-v4-pro",
            fast_model="deepseek-v4-flash",
            timeout_seconds=1,
        )
    )
    response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        client.chat_text([ChatMessage("user", "hi")], temperature=0.2)

    payload = json.loads(urlopen.call_args.args[0].data)
    assert urlopen.call_args.args[0].full_url == "https://gateway.llmgtw.io/v1/chat/completions"
    assert payload["model"] == "deepseek-v4-pro"
    assert "thinking" not in payload
    assert payload["temperature"] == 0.2


def test_thinking_mode_enabled_omits_temperature() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="https://deepseek.local",
            chat_model="deepseek-chat",
            fast_model="deepseek-chat",
            timeout_seconds=1,
            enable_thinking=True,
        )
    )
    response = FakeResponse({"choices": [{"message": {"content": "ok", "reasoning_content": "hidden"}}]})

    with patch("urllib.request.urlopen", return_value=response) as urlopen:
        assert client.chat_text([ChatMessage("user", "hi")], temperature=0.2) == "ok"

    payload = json.loads(urlopen.call_args.args[0].data)
    assert payload["thinking"] == {"type": "enabled"}
    assert "temperature" not in payload


def test_litellm_trace_metadata_is_forwarded_when_present() -> None:
    client = DeepSeekClient(
        DeepSeekSettings(
            api_key="sk-test",
            base_url="http://litellm:4000/v1",
            chat_model="aegora-chat",
            fast_model="aegora-fast",
            timeout_seconds=1,
        )
    )
    response = FakeResponse({"choices": [{"message": {"content": "ok"}}]})

    with patch("aegora_runtime.deepseek.current_llm_metadata", return_value={"trace_id": "abc"}):
        with patch("urllib.request.urlopen", return_value=response) as urlopen:
            assert client.chat_text([ChatMessage("user", "hi")]) == "ok"

    payload = json.loads(urlopen.call_args.args[0].data)
    assert payload["metadata"] == {
        "trace_id": "abc",
        "generation_name": "aegora-runtime:aegora-chat",
        "aegora_model_alias": "aegora-chat",
    }
