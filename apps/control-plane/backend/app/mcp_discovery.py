import hashlib
import re
from datetime import timedelta
from typing import Any

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from . import config
from .models import MCPConnectionDefinition, ToolDefinition


READ_ONLY_NAME_TOKENS = {
    "around",
    "distance",
    "direction",
    "fetch",
    "geo",
    "geocode",
    "get",
    "ip_location",
    "list",
    "lookup",
    "query",
    "read",
    "regeocode",
    "route",
    "search",
    "text_search",
    "weather",
}
WRITE_NAME_TOKENS = {
    "book",
    "cancel",
    "create",
    "delete",
    "modify",
    "post",
    "publish",
    "remove",
    "send",
    "take_taxi",
    "update",
    "write",
}


def _tool_payload(remote_tool: object) -> dict[str, Any]:
    if isinstance(remote_tool, dict):
        return remote_tool
    model_dump = getattr(remote_tool, "model_dump", None)
    if callable(model_dump):
        return model_dump(by_alias=True, exclude_none=True)
    raise ValueError("MCP tools/list 返回了无法识别的工具")


def _tool_id(connection_id: str, remote_name: str) -> str:
    safe_name = re.sub(r"[^a-zA-Z0-9._-]+", "_", remote_name).strip("._-") or "tool"
    if safe_name != remote_name:
        digest = hashlib.sha256(remote_name.encode("utf-8")).hexdigest()[:8]
        safe_name = f"{safe_name}.{digest}"
    return f"mcp.{connection_id}.{safe_name}"


def _name_tokens(remote_name: str) -> set[str]:
    return {token for token in re.split(r"[^a-zA-Z0-9]+", remote_name.lower()) if token}


def _infer_read_only(remote_name: str, annotations: dict[str, Any]) -> bool:
    if "readOnlyHint" in annotations:
        return annotations.get("readOnlyHint") is True
    tokens = _name_tokens(remote_name)
    if tokens & WRITE_NAME_TOKENS:
        return False
    return bool(tokens & READ_ONLY_NAME_TOKENS)


def remote_tool_to_definition(
    connection: MCPConnectionDefinition,
    remote_tool: object,
) -> ToolDefinition:
    payload = _tool_payload(remote_tool)
    remote_name = str(payload.get("name") or "").strip()
    if not remote_name:
        raise ValueError("MCP 工具缺少 name")
    annotations = payload.get("annotations")
    annotations = annotations if isinstance(annotations, dict) else {}
    read_only = _infer_read_only(remote_name, annotations)
    idempotent = annotations.get("idempotentHint") is True or read_only
    destructive = annotations.get("destructiveHint", not read_only) is not False
    open_world = annotations.get("openWorldHint", True) is not False
    if read_only:
        side_effect_level = "external_read" if open_world else "none"
    elif destructive:
        side_effect_level = "destructive"
    elif open_world:
        side_effect_level = "external_write"
    else:
        side_effect_level = "internal_write"
    input_schema = payload.get("inputSchema")
    return ToolDefinition(
        tool_id=_tool_id(connection.connection_id, remote_name),
        name=str(payload.get("title") or remote_name),
        description=str(payload.get("description") or ""),
        status="active",
        source="mcp",
        runner_tool_id=connection.runner_tool_id,
        runner_name=remote_name,
        mcp_connection_id=connection.connection_id,
        mcp_connection=connection,
        version=f"mcp-v{connection.config_version}",
        read_only=read_only,
        idempotent=idempotent,
        parallel_safe=read_only and idempotent,
        requires_approval=not read_only,
        side_effect_level=side_effect_level,
        data_sensitivity="internal",
        network_access="external" if open_world else "internal_only",
        layer="integration",
        category="mcp_tool",
        namespace=connection.connection_id,
        group_id=f"mcp.{connection.connection_id}",
        group_name=connection.name,
        timeout_ms=int(connection.config.get("call_timeout_ms") or 30_000),
        input_schema=input_schema if isinstance(input_schema, dict) else {},
    )


async def _list_all_tools(session: ClientSession) -> list[object]:
    await session.initialize()
    remote_tools: list[object] = []
    cursor: str | None = None
    for _ in range(100):
        result = await session.list_tools(cursor=cursor)
        remote_tools.extend(result.tools)
        cursor = result.nextCursor
        if not cursor:
            return remote_tools
    raise RuntimeError("MCP tools/list 分页超过 100 页")


def _timeout_seconds(connection: MCPConnectionDefinition, key: str, fallback: int) -> float:
    return max(0.1, int(connection.config.get(key) or fallback) / 1000)


async def discover_tools(connection: MCPConnectionDefinition) -> list[ToolDefinition]:
    if connection.status != "active":
        raise ValueError("停用的 MCP 连接不能执行工具发现")
    connect_timeout = _timeout_seconds(connection, "connect_timeout_ms", 10_000)
    call_timeout = _timeout_seconds(connection, "call_timeout_ms", 30_000)
    remote_tools: list[object]
    with anyio.fail_after(connect_timeout + call_timeout):
        if connection.transport == "streamable_http":
            headers = {
                str(key): str(value)
                for key, value in (connection.config.get("headers") or {}).items()
            }
            bearer_env = connection.config.get("bearer_env")
            has_authorization_header = any(key.lower() == "authorization" for key in headers)
            if bearer_env and not has_authorization_header:
                token = config.env_value(str(bearer_env))
                if not token:
                    raise ValueError(f"MCP Bearer 环境变量未配置: {bearer_env}")
                headers["Authorization"] = f"Bearer {token}"
            timeout = httpx.Timeout(
                connect=connect_timeout,
                read=call_timeout,
                write=call_timeout,
                pool=connect_timeout,
            )
            async with httpx.AsyncClient(
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                async with streamable_http_client(
                    str(connection.config.get("url") or ""),
                    http_client=client,
                ) as (read, write, _):
                    async with ClientSession(
                        read,
                        write,
                        read_timeout_seconds=timedelta(seconds=call_timeout),
                    ) as session:
                        remote_tools = await _list_all_tools(session)
        else:
            selected_env = {}
            for key in connection.config.get("env_vars") or []:
                value = config.env_value(str(key))
                if not value:
                    raise ValueError(f"MCP stdio 环境变量未配置: {key}")
                selected_env[str(key)] = value
            params = StdioServerParameters(
                command=str(connection.config.get("command") or ""),
                args=[str(value) for value in connection.config.get("args") or []],
                cwd=connection.config.get("cwd") or None,
                env={**get_default_environment(), **selected_env},
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=call_timeout),
                ) as session:
                    remote_tools = await _list_all_tools(session)
    tools = [remote_tool_to_definition(connection, tool) for tool in remote_tools]
    tool_ids = [tool.tool_id for tool in tools]
    if len(tool_ids) != len(set(tool_ids)):
        raise ValueError("MCP tools/list 包含重复工具名称")
    return tools
