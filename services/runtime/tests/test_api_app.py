from __future__ import annotations

import importlib
import json
import sys
import urllib.error

from fastapi.testclient import TestClient


def load_api_app(monkeypatch):
    monkeypatch.setenv("AGENT_LOOP_MODE", "planner")
    sys.modules.pop("apps.api_app", None)
    return importlib.import_module("apps.api_app")


def test_api_chat_requires_optional_bearer_token(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.setenv("API_BEARER_TOKEN", "secret")
    client = TestClient(api_app.app)

    response = client.post("/v1/chat", json={"session_id": "s1", "message": "你好"})

    assert response.status_code == 401


def test_api_chat_returns_agent_response(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)

    def fake_handle_chat_turn(request, _deps, _settings):
        assert request.session_id == "s1"
        assert request.message == "你好"
        assert request.metadata == {"channel": "im"}
        return {
            "session_id": "s1",
            "user_id": "session-owner:s1",
            "assistant_message_id": 9,
            "answer": "你好，我是 AiCoin 客服助手。",
            "route": "chat",
            "status": "completed",
            "trace_id": "turn-1",
            "latency_ms": 12.3,
            "evidence": [],
            "assets": [],
            "flow": [],
        }

    monkeypatch.setattr(api_app, "handle_chat_turn", fake_handle_chat_turn)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/chat",
        json={"session_id": "s1", "message": "你好", "metadata": {"channel": "im"}},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "你好，我是 AiCoin 客服助手。"
    assert body["assistant_message_id"] == 9


def test_metrics_endpoint_exposes_prometheus_metrics(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    client = TestClient(api_app.app)

    client.get("/healthz")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "aegora_runtime_http_requests_total" in response.text
    assert 'path="/healthz"' in response.text


def test_openapi_schema_loads(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    client = TestClient(api_app.app)

    response = client.get("/openapi.json")

    assert response.status_code == 200
    assert response.json()["paths"]["/metrics"]["get"]["summary"] == "Prometheus Metrics"


def test_gateway_run_resolves_release_and_returns_configured_response(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    captured = {}

    def fake_resolve(agent_id, release_id, actor_id, channel):
        captured["resolve"] = (agent_id, release_id, actor_id, channel)
        return {
            "release": {"release_id": release_id, "agent_id": agent_id, "status": "published", "version": 1},
            "agent": {"system_prompt": "你是发布助手", "model": "deepseek-v4-pro"},
            "actor": {"actor_id": actor_id},
            "channel": channel,
            "tools": [],
            "tool_ids": [],
            "tool_scopes": {},
        }

    def fake_run_configured_turn(context, **kwargs):
        captured["run"] = (context, kwargs)
        return {
            "answer": "发布版回答",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-1",
            "tool_observations": [],
            "observability": [{"event": "agent_decision"}],
            "model_usage": {"total_calls": 1},
        }

    def fake_save_chat_turn(_settings, **kwargs):
        captured["saved"] = kwargs
        return "turn-1:assistant"

    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context", fake_resolve)
    monkeypatch.setattr(api_app, "run_configured_turn", fake_run_configured_turn)
    monkeypatch.setattr(api_app, "save_chat_turn", fake_save_chat_turn)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "你好",
            "session_id": "s1",
            "sender_uid": "user_1",
            "metadata": {"source": "test"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"]
    assert body["agent_id"] == "agent_1"
    assert body["release_id"] == "rel_1"
    assert body["session_id"] == "s1"
    assert body["answer"] == "发布版回答"
    assert body["flow"] == [{"event": "agent_decision"}]
    assert captured["resolve"] == ("agent_1", "rel_1", "u_1", "web_console")
    assert captured["run"][1]["message"] == "你好"
    assert captured["run"][1]["user_id"] == "user_1"
    assert captured["saved"]["user_id"] == "user_1"
    assert captured["saved"]["source"] == "gateway"


def test_gateway_run_posts_answer_to_reply_url(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    posted = {}

    def fake_resolve(agent_id, release_id, actor_id, channel):
        return {
            "release": {"release_id": release_id, "agent_id": agent_id, "status": "published", "version": 1},
            "agent": {"system_prompt": "你是发布助手", "model": "deepseek-v4-pro"},
            "actor": {"actor_id": actor_id},
            "channel": channel,
            "tools": [],
            "tool_ids": [],
            "tool_scopes": {},
        }

    def fake_run_configured_turn(_context, **_kwargs):
        return {
            "answer": "webhook 回答",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-webhook",
            "tool_observations": [],
            "observability": [],
            "model_usage": {},
        }

    def fake_urlopen(request, timeout):
        posted["url"] = request.full_url
        posted["timeout"] = timeout
        posted["headers"] = dict(request.header_items())
        posted["body"] = json.loads(request.data.decode("utf-8"))

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b""

        return Response()

    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context", fake_resolve)
    monkeypatch.setattr(api_app, "run_configured_turn", fake_run_configured_turn)
    monkeypatch.setattr(api_app.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "webhook",
            "message": "你好",
            "cid": "10000",
            "event_id": "evt_1",
            "mid": "msg_1",
            "reply_url": "https://frontend.example/replies/signed-token",
        },
    )

    assert response.status_code == 200
    assert posted["url"] == "https://frontend.example/replies/signed-token"
    assert posted["timeout"] == 10
    assert posted["headers"]["Content-type"] == "application/json"
    assert posted["body"]["cid"] == "10000"
    assert posted["body"]["content"] == "webhook 回答"
    assert posted["body"]["answer"] == "webhook 回答"
    assert posted["body"]["event_id"] == "evt_1"
    assert posted["body"]["mid"] == "msg_1"
    assert posted["body"]["trace_id"] == "turn-webhook"
    assert response.json()["answer"] == "webhook 回答"


def test_gateway_run_reply_url_failure_is_logged_without_failing_run(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    events = []

    def fake_resolve(agent_id, release_id, actor_id, channel):
        return {
            "release": {"release_id": release_id, "agent_id": agent_id, "status": "published", "version": 1},
            "agent": {"system_prompt": "你是发布助手", "model": "deepseek-v4-pro"},
            "actor": {"actor_id": actor_id},
            "channel": channel,
            "tools": [],
            "tool_ids": [],
            "tool_scopes": {},
        }

    def fake_run_configured_turn(_context, **_kwargs):
        return {"answer": "仍然同步返回", "route": "answer", "status": "completed", "trace_id": "turn-1"}

    def fake_log_event(_logger, _level, event, **fields):
        events.append({"event": event, **fields})

    def fake_urlopen(_request, timeout):
        raise urllib.error.URLError("connect failed")

    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context", fake_resolve)
    monkeypatch.setattr(api_app, "run_configured_turn", fake_run_configured_turn)
    monkeypatch.setattr(api_app, "log_event", fake_log_event)
    monkeypatch.setattr(api_app.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "webhook",
            "message": "你好",
            "reply_url": "https://frontend.example/replies/signed-token?token=secret#frag",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "仍然同步返回"
    logged = next(item for item in events if item["event"] == "gateway_reply_url_error")
    assert logged["reply_url"] == "https://frontend.example/replies/signed-token"
    assert "secret" not in str(logged)
    assert "frag" not in str(logged)
    assert logged["error_type"] == "URLError"


def test_gateway_run_validation_error_is_logged(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    events = []

    def fake_log_event(_logger, _level, event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(api_app, "log_event", fake_log_event)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "你好",
            "reply_url": "http://127.0.0.1:8000/respond/signed-token",
        },
    )

    assert response.status_code == 422
    logged = next(item for item in events if item["event"] == "api_request_validation_error")
    assert logged["path"] == "/v1/gateway/runs"
    assert "release_id" in str(logged["validation_errors"])
    assert '"agent_id":"agent_1"' in logged["body_preview"].replace(" ", "")
    assert "signed-token" not in logged["body_preview"]
    assert '"reply_url":"[redacted]"' in logged["body_preview"]


def test_gateway_run_explicit_422_is_logged(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    events = []

    def fake_log_event(_logger, _level, event, **fields):
        events.append({"event": event, **fields})

    monkeypatch.setattr(api_app, "log_event", fake_log_event)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "你好",
            "stream": True,
        },
    )

    assert response.status_code == 422
    logged = next(item for item in events if item["event"] == "api_http_error_422")
    assert logged["path"] == "/v1/gateway/runs"
    assert logged["detail"] == "首期 gateway 暂不支持 stream=true"


def test_gateway_run_runtime_403_is_logged(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    events = []

    def fake_log_event(_logger, _level, event, **fields):
        events.append({"event": event, **fields})

    def fake_resolve(*_args):
        raise api_app.runtime_context.RuntimeContextError("渠道未授权", status_code=403)

    monkeypatch.setattr(api_app, "log_event", fake_log_event)
    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context", fake_resolve)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/runs",
        json={
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "oa-dev-user",
            "channel": "oa",
            "message": "你好",
        },
    )

    assert response.status_code == 403
    logged = next(item for item in events if item["event"] == "api_http_error")
    assert logged["path"] == "/v1/gateway/runs"
    assert logged["status_code"] == 403
    assert logged["detail"] == "渠道未授权"
    assert '"actor_id":"oa-dev-user"' in logged["body_preview"]
    assert '"channel":"oa"' in logged["body_preview"]


def test_gateway_approval_decision_returns_resume_result(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    captured = {}

    def fake_decide_approval(approval_id, decision, **kwargs):
        captured["approval_id"] = approval_id
        captured["decision"] = decision
        return {
            "run_id": "run_1",
            "session_id": "s1",
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "answer": "审批后已执行",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-1",
            "approval": {"approval_id": approval_id, "status": "executed"},
            "tool_observations": [{"status": "approval_approved"}],
            "model_usage": {},
            "observability": [{"event": "approval_resumed"}],
        }

    monkeypatch.setattr(api_app, "decide_approval", fake_decide_approval)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/approvals/appr_1/decide",
        json={"decision": "approved", "decided_by": "u_reviewer", "comment": "同意"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["approval"] == {"approval_id": "appr_1", "status": "executed"}
    assert captured["approval_id"] == "appr_1"
    assert captured["decision"].decision == "approved"
    assert captured["decision"].decided_by == "u_reviewer"


def test_gateway_approval_decision_accepts_actor_id_alias(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    captured = {}

    def fake_decide_approval(approval_id, decision, **kwargs):
        captured["decision"] = decision
        return {
            "run_id": "run_1",
            "session_id": "s1",
            "agent_id": "agent_1",
            "release_id": "rel_1",
            "actor_id": "u_1",
            "channel": "web_console",
            "answer": "审批后已执行",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-1",
            "approval": {"approval_id": approval_id, "status": "executed"},
            "tool_observations": [],
            "model_usage": {},
            "observability": [],
        }

    monkeypatch.setattr(api_app, "decide_approval", fake_decide_approval)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/gateway/approvals/appr_1/decide",
        json={"decision": "approved", "actor_id": "u_console", "comment": ""},
    )

    assert response.status_code == 200
    assert captured["decision"].decided_by == "u_console"


def test_webhook_run_resolves_release_by_version_and_returns_response(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    captured = {}

    def fake_resolve(agent_id, version, actor_id, channel):
        captured["resolve"] = (agent_id, version, actor_id, channel)
        return {
            "release": {"release_id": "rel_v3", "agent_id": agent_id, "status": "published", "version": version},
            "agent": {"system_prompt": "你是 webhook 助手", "model": "deepseek-v4-pro"},
            "actor": {"actor_id": actor_id},
            "channel": channel,
            "tools": [],
            "tool_ids": [],
            "tool_scopes": {},
        }

    def fake_run_configured_turn(context, **kwargs):
        captured["run"] = (context, kwargs)
        return {
            "answer": "webhook 专用回答",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-hook-1",
            "tool_observations": [],
            "observability": [{"event": "agent_decision"}],
            "model_usage": {"total_calls": 1},
        }

    def fake_save_chat_turn(_settings, **kwargs):
        captured["saved"] = kwargs
        return "turn-hook-1:assistant"

    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context_by_version", fake_resolve)
    monkeypatch.setattr(api_app, "run_configured_turn", fake_run_configured_turn)
    monkeypatch.setattr(api_app, "save_chat_turn", fake_save_chat_turn)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/webhooks/runs",
        json={
            "agent_id": "agent_1",
            "version": 3,
            "channel": "oa",
            "cid": "chat_123",
            "sender_uid": "oa_user_1",
            "message": "你好",
            "metadata": {"source": "oa"},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == "agent_1"
    assert body["release_id"] == "rel_v3"
    assert body["version"] == 3
    assert body["session_id"] == "chat_123"
    assert body["user_id"] == "oa_user_1"
    assert body["answer"] == "webhook 专用回答"
    assert captured["resolve"] == ("agent_1", 3, "oa_user_1", "oa")
    assert captured["run"][1]["session_id"] == "chat_123"
    assert captured["run"][1]["user_id"] == "oa_user_1"
    assert captured["saved"]["session_id"] == "chat_123"
    assert captured["saved"]["user_id"] == "oa_user_1"
    assert captured["saved"]["source"] == "webhook"


def test_webhook_run_posts_reply_to_reply_url(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    posted = {}

    def fake_resolve(agent_id, version, actor_id, channel):
        return {
            "release": {"release_id": "rel_v2", "agent_id": agent_id, "status": "published", "version": version},
            "agent": {"system_prompt": "你是 webhook 助手", "model": "deepseek-v4-pro"},
            "actor": {"actor_id": actor_id},
            "channel": channel,
            "tools": [],
            "tool_ids": [],
            "tool_scopes": {},
        }

    def fake_run_configured_turn(_context, **_kwargs):
        return {
            "answer": "回调内容",
            "route": "answer",
            "status": "completed",
            "trace_id": "turn-hook-2",
            "tool_observations": [],
            "observability": [],
            "model_usage": {},
        }

    def fake_urlopen(request, timeout):
        posted["url"] = request.full_url
        posted["body"] = json.loads(request.data.decode("utf-8"))

        class Response:
            status = 204

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self):
                return b""

        return Response()

    monkeypatch.setattr(api_app.runtime_context, "resolve_runtime_context_by_version", fake_resolve)
    monkeypatch.setattr(api_app, "run_configured_turn", fake_run_configured_turn)
    monkeypatch.setattr(api_app.urllib.request, "urlopen", fake_urlopen)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/webhooks/runs",
        json={
            "agent_id": "agent_1",
            "version": 2,
            "channel": "oa",
            "cid": "chat_555",
            "sender_uid": "oa_user_2",
            "message": "你好",
            "event_id": "evt_2",
            "mid": "msg_2",
            "reply_url": "https://frontend.example/replies/webhook-token",
        },
    )

    assert response.status_code == 200
    assert posted["url"] == "https://frontend.example/replies/webhook-token"
    assert posted["body"]["cid"] == "chat_555"
    assert posted["body"]["sender_uid"] == "oa_user_2"
    assert posted["body"]["event_id"] == "evt_2"
    assert posted["body"]["mid"] == "msg_2"
    assert posted["body"]["content"] == "回调内容"


def test_webhook_run_accepts_deprecated_sender_user_alias(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)
    captured = {}

    def fake_run_configured_webhook_request(**kwargs):
        captured.update(kwargs)
        return (
            {"answer": "ok", "route": "answer", "status": "completed"},
            {"release_id": "rel_v1", "version": 1},
        )

    monkeypatch.setattr(api_app, "run_configured_webhook_request", fake_run_configured_webhook_request)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/webhooks/runs",
        json={
            "agent_id": "agent_1",
            "version": 1,
            "channel": "oa",
            "cid": "chat_legacy",
            "sender_user": "legacy_user",
            "message": "你好",
        },
    )

    assert response.status_code == 200
    assert captured["actor_id"] == "legacy_user"
    assert captured["user_id"] == "legacy_user"


def test_api_chat_stream_returns_sse_events(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)

    def fake_handle_chat_turn(request, _deps, _settings):
        request.stream_handler(
            {
                "type": "agent_decision",
                "message": "模型决策：直接对话",
                "payload": {"event": "agent_decision", "route": "chat"},
                "session_id": request.session_id,
            }
        )
        return {"answer": "你好", "route": "chat", "status": "completed"}

    monkeypatch.setattr(api_app, "handle_chat_turn", fake_handle_chat_turn)
    client = TestClient(api_app.app)

    response = client.post("/v1/chat/stream", json={"session_id": "s1", "message": "你好"})

    assert response.status_code == 200
    text = response.text
    assert "data:" in text
    assert "模型决策：直接对话" in text
    assert "event: done" in text


def test_wecom_aibot_uses_userid_as_session_and_returns_markdown(monkeypatch) -> None:
    api_app = load_api_app(monkeypatch)
    monkeypatch.delenv("API_BEARER_TOKEN", raising=False)

    def fake_handle_chat_turn(request, _deps, _settings):
        assert request.session_id == "USERID"
        assert request.message == "hello robot\n\n引用内容：这是今日的测试情况"
        assert request.source == "wecom"
        assert request.metadata["chatid"] == "CHATID"
        assert request.metadata["from_userid"] == "USERID"
        return {
            "session_id": "USERID",
            "user_id": "session-owner:USERID",
            "assistant_message_id": 10,
            "answer": "收到，我来处理。",
            "route": "chat",
            "status": "completed",
            "trace_id": "turn-wecom",
            "latency_ms": 20.0,
            "evidence": [],
            "assets": [],
            "flow": [],
        }

    monkeypatch.setattr(api_app, "handle_chat_turn", fake_handle_chat_turn)
    client = TestClient(api_app.app)

    response = client.post(
        "/v1/wecom/aibot",
        json={
            "msgid": "CAIQ16HMjQYY/NGagIOAgAMgq4KM0AI=",
            "aibotid": "AIBOTID",
            "chatid": "CHATID",
            "chattype": "group",
            "from": {"userid": "USERID"},
            "response_url": "RESPONSEURL",
            "msgtype": "text",
            "text": {"content": "@RobotA hello robot"},
            "quote": {
                "msgtype": "text",
                "text": {"content": "这是今日的测试情况"},
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "msgtype": "markdown",
        "markdown": {"content": "收到，我来处理。"},
    }
