import asyncio
import sys

from app.mcp_discovery import discover_tools
from app.mcp_discovery import remote_tool_to_definition
from app.models import MCPConnectionDefinition


def connection_fixture() -> MCPConnectionDefinition:
    return MCPConnectionDefinition(
        connection_id="search.public",
        name="公共搜索",
        status="active",
        transport="streamable_http",
        config={"url": "https://search.example.com/mcp", "call_timeout_ms": 30000},
        config_version=2,
        config_hash="sha256:v2",
    )


def test_remote_search_tool_without_annotations_uses_read_only_name_inference():
    tool = remote_tool_to_definition(
        connection_fixture(),
        {
            "name": "web-search",
            "description": "搜索公开网页",
            "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    )

    assert tool.tool_id == "mcp.search.public.web-search"
    assert tool.runner_name == "web-search"
    assert tool.source == "mcp"
    assert tool.read_only is True
    assert tool.idempotent is True
    assert tool.parallel_safe is True
    assert tool.requires_approval is False
    assert tool.side_effect_level == "external_read"
    assert tool.network_access == "external"


def test_remote_write_tool_without_annotations_stays_approval_required():
    tool = remote_tool_to_definition(
        connection_fixture(),
        {
            "name": "delete-place",
            "description": "删除地点",
            "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    )

    assert tool.read_only is False
    assert tool.idempotent is False
    assert tool.parallel_safe is False
    assert tool.requires_approval is True
    assert tool.side_effect_level == "destructive"


def test_remote_tool_annotations_map_to_runtime_safety_fields():
    tool = remote_tool_to_definition(
        connection_fixture(),
        {
            "name": "lookup",
            "title": "资料查询",
            "description": "查询内部资料",
            "inputSchema": {"type": "object"},
            "annotations": {
                "readOnlyHint": True,
                "idempotentHint": True,
                "destructiveHint": False,
                "openWorldHint": False,
            },
        },
    )

    assert tool.name == "资料查询"
    assert tool.read_only is True
    assert tool.idempotent is True
    assert tool.parallel_safe is True
    assert tool.requires_approval is False
    assert tool.side_effect_level == "none"
    assert tool.network_access == "internal_only"


def test_discover_tools_from_stdio_fastmcp_server():
    server_code = """
from mcp.server.fastmcp import FastMCP

server = FastMCP("Discovery Test")

@server.tool()
def echo(message: str) -> str:
    \"\"\"回显输入。\"\"\"
    return message

server.run()
"""
    connection = MCPConnectionDefinition(
        connection_id="test.stdio",
        name="本地测试",
        status="active",
        transport="stdio",
        config={
            "command": sys.executable,
            "args": ["-c", server_code],
            "connect_timeout_ms": 5000,
            "call_timeout_ms": 5000,
        },
        config_version=1,
        config_hash="sha256:test",
    )

    tools = asyncio.run(discover_tools(connection))

    assert [tool.runner_name for tool in tools] == ["echo"]
    assert tools[0].input_schema["required"] == ["message"]
