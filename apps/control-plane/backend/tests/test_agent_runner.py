from fastapi.testclient import TestClient
from types import SimpleNamespace
import pytest

from app import api, runner, tools
from app.models import AgentRunResponse


def stub_agent(monkeypatch, tool_runner=None, calls=None, tools=None):
    calls = calls if calls is not None else []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "load_agent",
        lambda _: {
            "owner_user_id": "u_1",
            "enabled": True,
            "name": "finance",
            "system_prompt": "你是财务助手",
            "model": "deepseek-v4-pro",
            "tools": tools or {"kb_read"},
            "snapshot_tools": tools or {"kb_read"},
            "channels": {"web_console"},
        },
    )
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {tool_id: {} for tool_id in (tools or {"kb_read"})},
    )
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "create_run", lambda *args: calls.append(("create", args)))
    monkeypatch.setattr(api.db, "update_run", lambda *args, **kwargs: calls.append(("update", args, kwargs)))
    monkeypatch.setattr(api.db, "record_tool_call", lambda *args: calls.append(("tool_log", args)))
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: calls.append(("permission", args)))
    if tool_runner:
        monkeypatch.setattr(runner, "execute_tool", tool_runner)
    return calls


def test_agent_run_injects_prompt_model_and_tools(monkeypatch):
    captured = {}
    stub_agent(monkeypatch, tool_runner=lambda tool_id, message: f"{tool_id}: {message}")

    def llm(messages, model=None):
        captured["messages"] = messages
        captured["model"] = model
        return "已读取知识库"

    monkeypatch.setattr(runner, "call_llm_messages", llm)
    response = TestClient(api.app).post(
        "/agents/agent_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "查收入",
            "tool_ids": ["kb_read"],
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "已读取知识库"
    assert response.json()["tool_calls"][0]["status"] == "succeeded"
    assert captured["model"] == "deepseek-v4-pro"
    assert captured["messages"][0] == {"role": "system", "content": "你是财务助手"}
    assert "kb_read: 查收入" in captured["messages"][-1]["content"]


def test_agent_run_rejects_tool_not_in_effective_tools(monkeypatch):
    calls = stub_agent(
        monkeypatch,
        calls=[],
        tools={"kb_read"},
        tool_runner=lambda *_: "should not run",
    )
    monkeypatch.setattr(runner, "call_llm_messages", lambda *_args, **_kwargs: "nope")

    response = TestClient(api.app).post(
        "/agents/agent_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "查收入",
            "tool_ids": ["sql_readonly"],
        },
    )

    assert response.status_code == 403
    assert ("permission", ("agent_1", "u_1", "tool_denied", "requested=sql_readonly")) in calls


def test_agent_run_keeps_going_when_tool_fails(monkeypatch):
    def broken_tool(_tool_id, _message):
        raise RuntimeError("tool unavailable")

    monkeypatch.setattr(runner, "call_llm_messages", lambda *_args, **_kwargs: "工具失败，已返回普通回答")
    stub_agent(monkeypatch, tool_runner=broken_tool)

    response = TestClient(api.app).post(
        "/agents/agent_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "查收入",
            "tool_ids": ["kb_read"],
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "工具失败，已返回普通回答"
    assert response.json()["tool_calls"] == [
        {"tool_id": "kb_read", "status": "failed", "result": "tool unavailable"}
    ]


def test_local_tools_are_safe_and_useful():
    assert '"result": 7.0' in tools.execute_tool("calculator", "1 + 2 * 3")
    assert '"characters": 5' in tools.execute_tool("text_stats", "hello")
    assert '"now":' in tools.execute_tool("time_now", "ignored")
    with pytest.raises(ValueError, match="算术表达式"):
        tools.execute_tool("calculator", "__import__('os').system('date')")


def test_tool_scope_validation_supports_tool_level_permission():
    assert tools.validate_tool_scope("legacy_tool", {}, {}) == {}
    with pytest.raises(ValueError, match="scope"):
        tools.validate_tool_scope("calculator", {}, {"actions": ["calculate"]})
    with pytest.raises(ValueError, match="不合法"):
        tools.validate_tool_scope("legacy_tool", {"actions": ["read"]}, {})


def test_agent_run_calls_calculator_tool(monkeypatch):
    captured = {}
    stub_agent(monkeypatch, tools={"calculator"})

    def llm(messages, model=None):
        captured["content"] = messages[-1]["content"]
        return "结果是 7。"

    monkeypatch.setattr(runner, "call_llm_messages", llm)
    response = TestClient(api.app).post(
        "/agents/agent_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "1 + 2 * 3",
            "tool_ids": ["calculator"],
        },
    )

    assert response.status_code == 200
    assert response.json()["tool_calls"][0]["status"] == "succeeded"
    assert '"result": 7.0' in captured["content"]


def test_gateway_runner_posts_runtime_payload_and_parses_response(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"answer":"gateway ok","tool_calls":[{"tool_id":"calculator",'
                b'"status":"succeeded","result":{"value":7}}]}'
            )

    def fake_urlopen(http_request, timeout):
        captured["url"] = http_request.full_url
        captured["timeout"] = timeout
        captured["body"] = http_request.data.decode("utf-8")
        return FakeResponse()

    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: SimpleNamespace(
            runner_gateway_url="http://runner.local:5000",
            runner_gateway_timeout_seconds=6,
        ),
    )
    monkeypatch.setattr(runner.request, "urlopen", fake_urlopen)

    result = runner.run_gateway_once({"message": "1 + 2 * 3"}, fallback_run_id="run_1")

    assert captured["url"] == "http://runner.local:5000/v1/gateway/runs"
    assert captured["timeout"] == 6
    assert '"message": "1 + 2 * 3"' in captured["body"]
    assert result.run_id == "run_1"
    assert result.answer == "gateway ok"
    assert result.status == "succeeded"
    assert [trace.model_dump() for trace in result.tool_calls] == [
        {"tool_id": "calculator", "status": "succeeded", "result": '{"value": 7}'}
    ]


def test_gateway_runner_preserves_pending_approval(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return (
                b'{"status":"pending_approval","approval_requests":['
                b'{"approval_id":"approval_1","tool_id":"mcp.gaode.maps","reason":"external write"}]}'
            )

    monkeypatch.setattr(
        runner,
        "get_config",
        lambda: SimpleNamespace(
            runner_gateway_url="http://runner.local:5000",
            runner_gateway_timeout_seconds=6,
        ),
    )
    monkeypatch.setattr(runner.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    result = runner.run_gateway_once({"message": "需要审批"}, fallback_run_id="run_1")

    assert result.status == "pending_approval"
    assert result.answer == "等待人工审批"
    assert result.approval_requests[0]["approval_id"] == "approval_1"


def test_gateway_runner_extracts_pending_approval_from_tool_calls(monkeypatch):
    response = {
        "status": "pending_approval",
        "answer": "该操作需要审批后继续执行。",
        "tool_calls": [
            {"tool_id": "getBaziDetail", "status": "approval_approved", "result": ""},
            {"tool_id": "getBaziDetail", "status": "error", "result": "工具失败"},
            {
                "tool_id": "getBaziDetail",
                "status": "pending_approval",
                "approval_id": "approval_2",
                "reason": "重试工具调用需要再次审批",
                "arguments": {"name": "测试"},
            },
        ],
    }

    result = runner.parse_gateway_response(response, fallback_run_id="run_1")

    assert result.status == "pending_approval"
    assert result.approval_requests == [
        {
            "approval_id": "approval_2",
            "tool_id": "getBaziDetail",
            "reason": "重试工具调用需要再次审批",
            "arguments": {"name": "测试"},
        }
    ]


def test_gateway_runner_extracts_pending_approval_from_tool_call_result_json():
    response = {
        "status": "pending_approval",
        "tool_calls": [
            {
                "tool_id": "getBaziDetail",
                "status": "pending_approval",
                "result": (
                    '{"approval_id":"approval_3","reason":"再次审批",'
                    '"arguments":{"retry":true}}'
                ),
            },
        ],
    }

    result = runner.parse_gateway_response(response, fallback_run_id="run_1")

    assert result.approval_requests[0]["approval_id"] == "approval_3"
    assert result.approval_requests[0]["tool_id"] == "getBaziDetail"


def test_gateway_runner_ignores_historical_tool_pending_after_completion():
    response = {
        "status": "succeeded",
        "answer": "已完成",
        "tool_calls": [
            {
                "tool_id": "getBaziDetail",
                "status": "pending_approval",
                "approval_id": "old_approval",
                "reason": "历史审批记录",
            },
            {
                "tool_id": "getBaziDetail",
                "status": "succeeded",
                "result": "ok",
            },
        ],
    }

    result = runner.parse_gateway_response(response, fallback_run_id="run_1")

    assert result.status == "succeeded"
    assert result.approval_requests == []


def test_agent_run_can_select_gateway_runner(monkeypatch):
    stub_agent(monkeypatch, tools={"calculator"})
    monkeypatch.setattr(api.db, "fetch_tools_by_ids", lambda _: {})

    response = TestClient(api.app).post(
        "/agents/agent_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "连通测试",
            "tool_ids": ["calculator"],
            "runner_backend": "gateway",
        },
    )

    assert response.status_code == 502
    assert "真实 Gateway Runner 需要选择发布版本运行" in response.json()["detail"]


def test_agent_release_run_can_select_gateway_runner(monkeypatch):
    captured = {}
    calls = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "fetch_agent_release", lambda *_: api_test_release_fixture())
    monkeypatch.setattr(api.db, "agent_is_enabled", lambda _: True)
    monkeypatch.setattr(api.db, "active_tool_ids", lambda tool_ids: tool_ids)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(
        api.db,
        "get_user_role_tool_scopes",
        lambda _: {"calculator": {"actions": ["calculate"]}},
    )
    monkeypatch.setattr(api.db, "create_run", lambda *args: calls.append(("create", args)))
    monkeypatch.setattr(api.db, "update_run", lambda *args, **kwargs: calls.append(("update", args, kwargs)))
    monkeypatch.setattr(api.db, "record_tool_call", lambda *args: calls.append(("tool", args)))
    monkeypatch.setattr(api.db, "record_permission_event", lambda *args: calls.append(("permission", args)))

    def gateway(payload, fallback_run_id=""):
        captured["payload"] = payload
        return AgentRunResponse(
            run_id=fallback_run_id,
            answer="gateway answer",
            tool_calls=[],
        )

    monkeypatch.setattr(runner, "run_gateway_once", gateway)
    response = TestClient(api.app).post(
        "/agents/agent_1/releases/release_1/runs",
        json={
            "actor_id": "u_1",
            "channel": "web_console",
            "message": "连通测试",
            "tool_ids": ["calculator"],
            "runner_backend": "gateway",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "gateway answer"
    assert captured["payload"]["agent_id"] == "agent_1"
    assert captured["payload"]["release_id"] == "release_1"
    assert captured["payload"]["message"] == "连通测试"
    assert captured["payload"]["metadata"]["allowed_tool_ids"] == ["calculator"]
    assert captured["payload"]["metadata"]["runtime_context"]["tool_ids"] == ["calculator"]


def api_test_release_fixture():
    return {
        "release_id": "release_1",
        "agent_id": "agent_1",
        "version": 1,
        "status": "published",
        "published_by": "u_1",
        "published_at": "2026-06-25 10:00:00+08",
        "revoked_at": None,
        "config_json": {
            "agent": {
                "id": "agent_1",
                "name": "calculator-agent",
                "icon": "calculator",
                "visibility": "private",
                "members": [],
                "owner_user_id": "u_1",
                "system_prompt": "执行计算",
                "model": None,
                "channels": ["web_console"],
                "enabled": True,
            },
            "tools": [
                {
                    "tool_id": "calculator",
                    "runner_tool_id": "local.calculator",
                    "runner_name": None,
                    "name": "Calculator",
                    "description": "执行安全的基础算术表达式。",
                    "source": "local",
                    "version": "local-v1",
                    "read_only": True,
                    "idempotent": True,
                    "parallel_safe": True,
                    "requires_approval": False,
                    "side_effect_level": "none",
                    "data_sensitivity": "internal",
                    "network_access": "none",
                    "timeout_ms": 8000,
                    "input_schema": {"type": "object"},
                    "scope_schema": {"actions": ["calculate"]},
                    "scope": {"actions": ["calculate"]},
                    "manifest_hash": "sha256:calculator",
                }
            ],
        },
    }


def test_gateway_payload_filters_context_to_allowed_tools():
    payload = api.gateway_run_payload(
        "run_1",
        "agent_1",
        "hello",
        SimpleNamespace(
            actor_id="u_1",
            channel="web_console",
            session_id=None,
        ),
        {
            "release": {"release_id": "release_1"},
            "tools": [
                {"tool_id": "calculator"},
                {"tool_id": "admin_tool"},
            ],
            "tool_ids": ["admin_tool", "calculator"],
            "tool_scopes": {"calculator": {}, "admin_tool": {}},
        },
        requested_tools=["calculator"],
        allowed_tools={"calculator"},
    )

    assert payload["agent_id"] == "agent_1"
    assert payload["release_id"] == "release_1"
    assert payload["metadata"]["runtime_context"]["tools"] == [{"tool_id": "calculator"}]
    assert payload["metadata"]["runtime_context"]["tool_ids"] == ["calculator"]
    assert payload["metadata"]["runtime_context"]["tool_scopes"] == {"calculator": {}}


def test_gateway_payload_rejects_draft_context():
    with pytest.raises(RuntimeError, match="发布版本"):
        api.gateway_run_payload(
            "run_1",
            "agent_1",
            "hello",
            SimpleNamespace(
                actor_id="u_1",
                channel="web_console",
                session_id=None,
            ),
            {"release": None, "tools": [], "tool_ids": [], "tool_scopes": {}},
            requested_tools=[],
            allowed_tools=set(),
        )
