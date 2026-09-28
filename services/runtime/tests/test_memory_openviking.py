from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from aegora_runtime.config import load_settings
from aegora_runtime.memory.models import MemoryScope, MemoryWrite
from aegora_runtime.memory.openviking import (
    OpenVikingHttpClient,
    OpenVikingMemoryProvider,
)


class FakeOpenVikingClient:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str, str], str] = {}
        self.calls: list[dict] = []

    @staticmethod
    def _doc_key(scope, uri: str) -> tuple[str, str, str]:
        return (str(scope.tenant_id or "default"), scope.user_id, uri)

    def request(self, *, scope, method, path, payload=None, query=None):
        self.calls.append(
            {
                "scope": scope,
                "method": method,
                "path": path,
                "payload": payload,
                "query": query,
            }
        )
        if method == "GET" and path == "/api/v1/content/read":
            uri = query["uri"]
            key = self._doc_key(scope, uri)
            if key not in self.documents:
                from aegora_runtime.memory.openviking import OpenVikingNotFound

                raise OpenVikingNotFound(uri)
            return self.documents[key]

        if method == "POST" and path == "/api/v1/content/write":
            self.documents[self._doc_key(scope, payload["uri"])] = payload["content"]
            return {"uri": payload["uri"]}

        if method == "POST" and path == "/api/v1/search/find":
            target = payload["target_uri"]
            identity = (str(scope.tenant_id or "default"), scope.user_id)
            entries = [
                {"uri": f"{target.rstrip('/')}/.overview.md", "score": 0.99}
            ] + [
                {"uri": uri, "score": 0.9}
                for (tenant_id, user_id, uri) in self.documents
                if (tenant_id, user_id) == identity and uri.startswith(target)
            ]
            return {"memories": entries}

        if method == "DELETE" and path == "/api/v1/fs":
            self.documents.pop(self._doc_key(scope, query["uri"]), None)
            return {"removed": True}

        if method == "GET" and path == "/health":
            return {"healthy": True}
        raise AssertionError(f"unexpected call: {method} {path}")


def settings():
    return load_settings(env_path=None)


def scope(*, user_id="u1", agent_id="agent-a", memory_scope="user_global"):
    return MemoryScope(
        user_id=user_id,
        session_id="s1",
        agent_id=agent_id,
        tenant_id="tenant-a",
        namespace="preferences",
        memory_scope=memory_scope,
    )


def test_openviking_provider_remember_versions_recall_and_forget() -> None:
    client = FakeOpenVikingClient()
    provider = OpenVikingMemoryProvider(settings(), client=client)
    memory_scope = scope()

    v1 = provider.remember(
        scope=memory_scope,
        memory=MemoryWrite(
            key="preferred_language",
            value="Python",
            source_agent_id="agent-a",
            source_trace_id="trace-1",
            source_kind="explicit",
        ),
    )
    v2 = provider.remember(
        scope=memory_scope,
        memory=MemoryWrite(
            key="preferred_language",
            value="Go",
            source_agent_id="agent-a",
            source_trace_id="trace-2",
            source_kind="automatic",
            confidence=0.99,
        ),
    )
    recalled = provider.recall(
        scope=memory_scope,
        query="language",
        limit=8,
    )

    assert v1.version == 1
    assert v2.version == 2
    assert v2.metadata["supersedes_id"] == v1.memory_id
    assert [(item.key, item.content, item.version) for item in recalled] == [
        ("preferred_language", "Go", 2)
    ]
    assert recalled[0].metadata["provider_score"] == 0.9
    search_call = next(
        call for call in client.calls
        if call["method"] == "POST" and call["path"] == "/api/v1/search/find"
    )
    assert search_call["payload"]["context_type"] == ["memory"]
    assert search_call["payload"]["target_uri"].endswith("/")

    write_calls = [
        call for call in client.calls
        if call["method"] == "POST" and call["path"] == "/api/v1/content/write"
    ]
    assert len(write_calls) == 2
    assert write_calls[-1]["payload"]["mode"] == "replace"
    assert write_calls[-1]["payload"]["uri"].startswith(
        "viking://~/memories/preferences/aegora/"
    )
    assert "/preferences-" in write_calls[-1]["payload"]["uri"]
    assert write_calls[-1]["payload"]["uri"].endswith(".md")

    assert provider.forget(
        scope=memory_scope,
        key="preferred_language",
        reason="explicit forget",
    ) is True
    assert provider.get_current(
        scope=memory_scope,
        key="preferred_language",
    ) is None
    assert provider.forget(
        scope=memory_scope,
        key="preferred_language",
    ) is False


def test_openviking_provider_filters_expired_memories_on_recall() -> None:
    client = FakeOpenVikingClient()
    provider = OpenVikingMemoryProvider(settings(), client=client)
    memory_scope = scope()
    provider.remember(
        scope=memory_scope,
        memory=MemoryWrite(
            key="temporary",
            value="old",
            source_kind="automatic",
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    )

    assert provider.recall(
        scope=memory_scope,
        query="temporary",
        limit=8,
    ) == []


def test_openviking_provider_agent_overlay_isolated() -> None:
    client = FakeOpenVikingClient()
    provider = OpenVikingMemoryProvider(settings(), client=client)
    coding_scope = scope(agent_id="coding-agent", memory_scope="user_agent")
    hr_scope = scope(agent_id="hr-agent", memory_scope="user_agent")

    provider.remember(
        scope=coding_scope,
        memory=MemoryWrite(
            key="test_framework",
            value="pytest",
            source_agent_id="coding-agent",
        ),
    )

    assert provider.get_current(
        scope=hr_scope,
        key="test_framework",
    ) is None
    assert provider.recall(
        scope=hr_scope,
        query="pytest",
        limit=8,
    ) == []


def test_openviking_provider_user_identity_produces_distinct_uri() -> None:
    client = FakeOpenVikingClient()
    provider = OpenVikingMemoryProvider(settings(), client=client)

    item_a = provider.remember(
        scope=scope(user_id="u-a"),
        memory=MemoryWrite(key="style", value="concise"),
    )
    item_b = provider.remember(
        scope=scope(user_id="u-b"),
        memory=MemoryWrite(key="style", value="verbose"),
    )

    assert item_a.memory_id != item_b.memory_id
    assert provider.get_current(scope=scope(user_id="u-a"), key="style").content == "concise"
    assert provider.get_current(scope=scope(user_id="u-b"), key="style").content == "verbose"


def test_openviking_expire_due_is_safe_noop_without_user_scope() -> None:
    provider = OpenVikingMemoryProvider(settings(), client=FakeOpenVikingClient())

    assert provider.expire_due(tenant_id="tenant-a", limit=100) == 0


class FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.payload


def test_openviking_http_client_trusted_mode_uses_server_side_identity_headers() -> None:
    configured = replace(
        settings(),
        memory=replace(
            settings().memory,
            openviking_auth_mode="trusted",
            openviking_api_key="ov-secret",
        ),
    )
    client = OpenVikingHttpClient(configured)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHttpResponse({"status": "ok", "result": {"healthy": True}})

    with patch("aegora_runtime.memory.openviking.urlopen", fake_urlopen):
        result = client.request(
            scope=scope(),
            method="GET",
            path="/health",
        )

    headers = {key.lower(): value for key, value in captured["request"].header_items()}
    assert result == {"healthy": True}
    assert headers["x-api-key"] == "ov-secret"
    assert headers["x-openviking-account"] == "tenant-a"
    assert headers["x-openviking-user"] == "u1"
    assert headers["x-openviking-actor-peer"] == "agent-a"
    assert captured["timeout"] == configured.memory.openviking_timeout_seconds


def test_openviking_http_client_api_key_mode_does_not_send_identity_headers() -> None:
    configured = replace(
        settings(),
        memory=replace(
            settings().memory,
            openviking_auth_mode="api_key",
            openviking_api_key="ov-secret",
        ),
    )
    client = OpenVikingHttpClient(configured)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        return FakeHttpResponse({"status": "ok", "result": {}})

    with patch("aegora_runtime.memory.openviking.urlopen", fake_urlopen):
        client.request(
            scope=scope(),
            method="GET",
            path="/health",
        )

    headers = {key.lower(): value for key, value in captured["request"].header_items()}
    assert headers["x-api-key"] == "ov-secret"
    assert "x-openviking-account" not in headers
    assert "x-openviking-user" not in headers
