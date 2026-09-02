from app import runtime_context


def release_fixture():
    return {
        "release_id": "release_1",
        "agent_id": "agent_1",
        "version": 2,
        "status": "published",
        "config_json": {
            "agent": {
                "id": "agent_1",
                "name": "office-agent",
                "icon": "calculator",
                "visibility": "private",
                "members": ["u_member"],
                "owner_user_id": "u_1",
                "system_prompt": "执行计算",
                "model": "deepseek-v4-pro",
                "enabled": True,
                "channels": ["web_console", "im"],
            },
            "tools": [
                {
                    "tool_id": "calculator",
                    "runner_tool_id": "local.calculator",
                    "scope": {"actions": ["calculate"]},
                },
                {
                    "tool_id": "time_now",
                    "runner_tool_id": "local.time_now",
                    "scope": {"actions": ["read"]},
                },
            ],
        },
    }


def test_build_runtime_context_filters_disabled_tools(monkeypatch):
    monkeypatch.setattr(runtime_context.db, "active_tool_ids", lambda _: {"calculator"})

    context = runtime_context.build_runtime_context(
        release_fixture(), actor_id="u_1", channel="web_console"
    )

    assert context["release"]["release_id"] == "release_1"
    assert context["agent"]["system_prompt"] == "执行计算"
    assert context["agent"]["visibility"] == "private"
    assert context["agent"]["members"] == ["u_member"]
    assert context["actor"] == {"actor_id": "u_1"}
    assert context["channel"] == "web_console"
    assert context["tool_ids"] == ["calculator"]
    assert context["tool_scopes"] == {"calculator": {"actions": ["calculate"]}}
    assert context["policy"]["disabled_tools_filtered"] == ["time_now"]


def test_runner_agent_from_context_matches_debug_runner_shape(monkeypatch):
    monkeypatch.setattr(runtime_context.db, "active_tool_ids", lambda tool_ids: tool_ids)
    context = runtime_context.build_runtime_context(release_fixture())

    agent = runtime_context.runner_agent_from_context(context)

    assert agent["owner_user_id"] == "u_1"
    assert agent["visibility"] == "private"
    assert agent["members"] == {"u_member"}
    assert agent["model"] == "deepseek-v4-pro"
    assert agent["tools"] == {"calculator", "time_now"}
    assert agent["channels"] == {"web_console", "im"}
    assert agent["release_id"] == "release_1"
