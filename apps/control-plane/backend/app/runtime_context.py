from . import db
from .authz import normalize_scope


def release_tool_ids(config_json: dict[str, object]) -> set[str]:
    tools = config_json.get("tools") or []
    return {
        str(tool.get("tool_id"))
        for tool in tools
        if isinstance(tool, dict) and tool.get("tool_id")
    }


def build_runtime_context(
    release: dict[str, object],
    actor_id: str | None = None,
    channel: str | None = None,
) -> dict[str, object]:
    config_json = release["config_json"]
    if not isinstance(config_json, dict):
        raise ValueError("release config_json 不合法")
    agent_config = config_json.get("agent")
    if not isinstance(agent_config, dict):
        raise ValueError("release agent 配置缺失")

    raw_tools = config_json.get("tools") or []
    if not isinstance(raw_tools, list):
        raise ValueError("release tools 配置不合法")

    released_tool_ids = release_tool_ids(config_json)
    active_tool_ids = db.active_tool_ids(released_tool_ids)
    active_tool_configs = [
        tool
        for tool in raw_tools
        if isinstance(tool, dict) and tool.get("tool_id") in active_tool_ids
    ]
    tool_scopes = {
        str(tool["tool_id"]): normalize_scope(tool.get("scope"))
        for tool in active_tool_configs
    }

    return {
        "release": {
            "release_id": release["release_id"],
            "agent_id": release["agent_id"],
            "version": release["version"],
            "status": release["status"],
        },
        "agent": {
            "id": agent_config.get("id"),
            "name": agent_config.get("name") or "released-agent",
            "icon": agent_config.get("icon") or "robot",
            "visibility": agent_config.get("visibility") or "private",
            "members": list(agent_config.get("members") or []),
            "owner_user_id": agent_config.get("owner_user_id"),
            "system_prompt": agent_config.get("system_prompt") or "",
            "model": agent_config.get("model"),
            "enabled": bool(agent_config.get("enabled", True)),
            "channels": list(agent_config.get("channels") or []),
        },
        "actor": {"actor_id": actor_id},
        "channel": channel,
        "tools": active_tool_configs,
        "tool_ids": sorted(tool_scopes),
        "tool_scopes": tool_scopes,
        "policy": {
            "source": "agent_release",
            "disabled_tools_filtered": sorted(released_tool_ids - active_tool_ids),
        },
    }


def runner_agent_from_context(context: dict[str, object]) -> dict[str, object]:
    agent = context["agent"]
    if not isinstance(agent, dict):
        raise ValueError("runtime context agent 不合法")
    tool_scopes = context["tool_scopes"]
    if not isinstance(tool_scopes, dict):
        raise ValueError("runtime context tool_scopes 不合法")
    release = context["release"]
    if not isinstance(release, dict):
        raise ValueError("runtime context release 不合法")

    return {
        "owner_user_id": agent.get("owner_user_id"),
        "enabled": bool(agent.get("enabled", True)),
        "name": agent.get("name"),
        "icon": agent.get("icon"),
        "visibility": agent.get("visibility") or "private",
        "members": set(agent.get("members") or []),
        "system_prompt": agent.get("system_prompt") or "",
        "model": agent.get("model"),
        "tools": set(tool_scopes),
        "tool_scopes": tool_scopes,
        "snapshot_tools": set(tool_scopes),
        "channels": set(agent.get("channels") or []),
        "release_id": release["release_id"],
        "release_version": release["version"],
    }


def fetch_runtime_context(
    agent_id: str,
    release_id: str,
    actor_id: str | None = None,
    channel: str | None = None,
) -> dict[str, object] | None:
    release = db.fetch_agent_release(agent_id, release_id)
    if release is None:
        return None
    return build_runtime_context(release, actor_id=actor_id, channel=channel)
