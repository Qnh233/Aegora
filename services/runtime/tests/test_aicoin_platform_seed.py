from __future__ import annotations

from aegora_runtime.aicoin_platform_seed import AGENT_ID, ROLE_ID, release_config, tool_definition, tool_manifests


def test_aicoin_tool_manifests_cover_legacy_tools() -> None:
    tools = tool_manifests()

    assert [tool["tool_id"] for tool in tools] == [
        "runtime.load_skill",
        "support.search_faq",
        "support.lookup_faq_detail",
        "runtime.save_session_memory",
        "support.record_handoff",
    ]
    assert {tool["runner_tool_id"] for tool in tools} == {
        "local.load_skill",
        "local.search_faq",
        "local.lookup_faq_detail",
        "local.save_user_memory",
        "local.record_handoff",
    }
    assert tool_definition(tools[1])["manifest_hash"].startswith("sha256:")
    assert tool_definition(tools[1])["layer"] == "support"
    assert tool_definition(tools[0])["scope_schema"]["skill_library_ids"] == ["aicoin_customer_support"]
    assert tool_definition(tools[1])["scope_descriptions"]["faq_collections"]


def test_release_config_contains_agent_tools_and_permission_snapshot() -> None:
    config = release_config(AGENT_ID, "u_console", tool_manifests())

    assert config["agent"]["id"] == AGENT_ID
    assert config["agent"]["owner_user_id"] == "u_console"
    assert config["agent"]["visibility"] == "private"
    assert "web_console" in config["agent"]["channels"]
    assert [tool["tool_id"] for tool in config["tools"]] == [
        "runtime.load_skill",
        "runtime.save_session_memory",
        "support.lookup_faq_detail",
        "support.record_handoff",
        "support.search_faq",
    ]
    assert config["permission_snapshot"]["published_by_roles"] == [ROLE_ID]
    assert config["permission_snapshot"]["role_tool_scopes"]["support.search_faq"]["faq_collections"] == ["aicoin_faq"]
