from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from fastmcp import FastMCP

from aegora_runtime.config import load_settings
from aegora_runtime.local_aicoin_tools import AICOIN_LOCAL_TOOL_IDS, build_aicoin_tool_runtime
from aegora_runtime.real_agent import LazyBgeM3Encoder, search_faq_tool
from aegora_runtime.registry import registry


mcp = FastMCP("agentic-rag-local-tools")
SETTINGS = load_settings()
ENCODER = LazyBgeM3Encoder(SETTINGS)


def build_tool_state(payload: dict[str, Any]) -> dict[str, Any]:
    state = payload.get("state") if isinstance(payload.get("state"), dict) else {}
    request_data = state.get("request") if isinstance(state.get("request"), dict) else {}
    request = SimpleNamespace(**request_data)
    runtime = build_aicoin_tool_runtime(
        SETTINGS,
        lambda args, tool_state: search_faq_tool(args, tool_state, ENCODER, SETTINGS),
    )
    return {
        **state,
        "request": request,
        "_tool_runtime": runtime,
    }


def register_local_mcp_tool(tool_id: str) -> None:
    local_tool = registry.get_tool(f"local.{tool_id}")

    async def handler(payload: dict[str, Any]) -> dict[str, Any]:
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        return local_tool.handler(args, build_tool_state(payload))

    handler.__name__ = f"{tool_id}_handler"
    mcp.tool(name=tool_id, description=local_tool.description)(handler)


for item in AICOIN_LOCAL_TOOL_IDS:
    register_local_mcp_tool(item)


def main() -> None:
    mcp.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
