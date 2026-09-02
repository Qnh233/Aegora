from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse

from aegora_runtime.registry import RunnerRegistry, registry as default_registry
from aegora_runtime.tools import RuntimeToolExecutor, ToolMetadata, ToolSpec


class ToolAdapterError(RuntimeError):
    pass


@dataclass
class MCPClientSession:
    client: Any
    lock: asyncio.Lock
    cache_key: str
    connection_id: str
    created_at: float
    last_used_at: float
    idle_ttl_seconds: float
    discovery_ttl_seconds: float
    discovered_at: float = 0.0
    discovered_tools: list[str] | None = None


def build_runtime_tool_executor(
    runtime_context: dict[str, object],
    *,
    runner_registry: RunnerRegistry | None = None,
    tool_state: dict[str, Any] | None = None,
) -> RuntimeToolExecutor:
    executor = RuntimeToolExecutor()
    local = runner_registry or default_registry
    for tool in runtime_tools(runtime_context):
        executor.register(tool_spec_from_config(tool, local, tool_state or {}))
    return executor


def runtime_tools(runtime_context: dict[str, object]) -> list[dict[str, Any]]:
    tools = runtime_context.get("tools") or []
    if not isinstance(tools, list):
        raise ToolAdapterError("runtime context tools 不合法")
    return [dict(tool) for tool in tools if isinstance(tool, dict) and tool.get("tool_id")]


def tool_spec_from_config(
    tool: dict[str, Any],
    runner_registry: RunnerRegistry,
    tool_state: dict[str, Any],
) -> ToolSpec:
    tool_id = str(tool["tool_id"])
    metadata = ToolMetadata(
        read_only=bool(tool.get("read_only", True)),
        parallel_safe=bool(tool.get("parallel_safe", True)),
        idempotent=bool(tool.get("idempotent", True)),
        side_effects=str(tool.get("side_effect_level") or "unspecified"),
    )

    return ToolSpec(
        name=tool_id,
        description=str(tool.get("description") or tool.get("name") or tool_id),
        input_schema={},
        # ToolSpec.handler 是统一执行层调用到真实工具实现的唯一跳板。
        handler=lambda args, state, manifest=tool: execute_tool_manifest(
            manifest,
            args,
            state,
            runner_registry,
            tool_state,
        ),
        metadata=metadata,
        json_schema=tool.get("input_schema") if isinstance(tool.get("input_schema"), dict) else None,
    )


def execute_tool_manifest(
    tool: dict[str, Any],
    args: dict[str, Any],
    state: dict[str, Any],
    runner_registry: RunnerRegistry,
    tool_state: dict[str, Any],
) -> dict[str, Any]:
    # 这里是真实工具调用分发点：local 走进程内 registry，MCP 走连接管理器。
    validate_json_schema_args(str(tool["tool_id"]), tool.get("input_schema"), args)
    runner_tool_id = str(tool.get("runner_tool_id") or f"local.{tool['tool_id']}")
    if runner_tool_id.startswith("local."):
        # 本地工具真正实现：@registry.tool 注册的 handler。
        return runner_registry.get_tool(runner_tool_id).handler(args, {**state, "_tool_runtime": tool_state, "_current_tool": tool})
    if isinstance(tool.get("mcp_connection"), dict):
        return call_mcp_tool_sync(tool, args, state)
    if runner_tool_id.startswith("mcp+http://") or runner_tool_id.startswith("mcp+https://"):
        return call_mcp_tool_sync(tool, args, state)
    if runner_tool_id.startswith("mcp+stdio://"):
        return call_mcp_tool_sync(tool, args, state)
    raise ToolAdapterError(f"unsupported runner_tool_id: {runner_tool_id}")


def warmup_runtime_mcp_clients(
    runtime_context: dict[str, object],
    *,
    manager: "MCPClientManager | None" = None,
) -> dict[str, dict[str, Any]]:
    active_manager = manager or mcp_client_manager
    results: dict[str, dict[str, Any]] = {}
    warmed_connections: set[str] = set()
    for tool in runtime_tools(runtime_context):
        runner_tool_id = str(tool.get("runner_tool_id") or "")
        if not is_mcp_tool(tool):
            continue
        cache_key = mcp_connection_cache_key(tool)
        if cache_key in warmed_connections:
            continue
        warmed_connections.add(cache_key)
        tool_id = str(tool.get("tool_id") or runner_tool_id)
        try:
            results[tool_id] = active_manager.warmup(tool)
        except Exception as exc:
            results[tool_id] = {
                "status": "error",
                "runner_tool_id": runner_tool_id,
                "connection_id": mcp_connection_id(tool),
                "error": str(exc),
            }
    return results


def is_mcp_tool(tool: dict[str, Any]) -> bool:
    if isinstance(tool.get("mcp_connection"), dict):
        return True
    return is_mcp_runner_tool_id(str(tool.get("runner_tool_id") or ""))


def is_mcp_runner_tool_id(runner_tool_id: str) -> bool:
    return runner_tool_id.startswith(("mcp+http://", "mcp+https://", "mcp+stdio://"))


def normalize_mcp_tool(tool_or_runner_id: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(tool_or_runner_id, dict):
        return tool_or_runner_id
    return {"runner_tool_id": tool_or_runner_id}


def mcp_connection_id(tool: dict[str, Any]) -> str:
    connection = tool.get("mcp_connection")
    if isinstance(connection, dict) and connection.get("connection_id"):
        return str(connection["connection_id"])
    return str(tool.get("mcp_connection_id") or tool.get("runner_tool_id") or "")


def mcp_connection_cache_key(tool: dict[str, Any]) -> str:
    connection = tool.get("mcp_connection")
    if not isinstance(connection, dict):
        return str(tool.get("runner_tool_id") or "")
    connection_id = mcp_connection_id(tool)
    config_hash = str(connection.get("config_hash") or "")
    if not config_hash:
        serialized = json.dumps(connection, sort_keys=True, ensure_ascii=True, default=str)
        config_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    version = str(connection.get("config_version") or "0")
    credential_version = str(connection.get("credential_version") or "0")
    return f"{connection_id}:{config_hash}:{version}:{credential_version}"


def mcp_connection_config(tool: dict[str, Any]) -> dict[str, Any]:
    connection = tool.get("mcp_connection")
    if not isinstance(connection, dict):
        return {}
    config = connection.get("config")
    return config if isinstance(config, dict) else {}


def mcp_connect_timeout_seconds(tool_or_runner_id: dict[str, Any] | str) -> float:
    tool = normalize_mcp_tool(tool_or_runner_id)
    value = mcp_connection_config(tool).get("connect_timeout_ms")
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000
    return 10.0


def mcp_idle_ttl_seconds(tool: dict[str, Any], default: float) -> float:
    value = mcp_connection_config(tool).get("idle_ttl_seconds")
    return float(value) if isinstance(value, (int, float)) and value > 0 else default


def mcp_discovery_ttl_seconds(tool: dict[str, Any], default: float) -> float:
    value = mcp_connection_config(tool).get("discovery_ttl_seconds")
    return float(value) if isinstance(value, (int, float)) and value > 0 else default


def env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def validate_json_schema_args(tool_id: str, schema: object, args: dict[str, Any]) -> None:
    if not isinstance(schema, dict) or not schema:
        return
    required = schema.get("required")
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and key not in args:
                raise ToolAdapterError(f"tool {tool_id} missing required arg: {key}")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for key, value in args.items():
        prop = properties.get(key)
        if isinstance(prop, dict) and isinstance(prop.get("type"), str):
            validate_json_type(tool_id, key, value, prop["type"])


def validate_json_type(tool_id: str, key: str, value: Any, json_type: str) -> None:
    type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    expected = type_map.get(json_type)
    if expected is None:
        return
    if json_type == "integer" and isinstance(value, bool):
        raise ToolAdapterError(f"tool {tool_id} arg {key} expects integer")
    if json_type == "number" and isinstance(value, bool):
        raise ToolAdapterError(f"tool {tool_id} arg {key} expects number")
    if not isinstance(value, expected):
        raise ToolAdapterError(f"tool {tool_id} arg {key} expects {json_type}")


def call_mcp_tool_sync(tool: dict[str, Any], args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return mcp_client_manager.call_tool(tool, args, state)


class MCPClientManager:
    def __init__(
        self,
        *,
        idle_ttl_seconds: float | None = None,
        discovery_ttl_seconds: float | None = None,
        cleanup_interval_seconds: float | None = None,
        max_sessions: int | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._sessions: dict[str, MCPClientSession] = {}
        self._sessions_lock: asyncio.Lock | None = None
        self._clock = clock
        self._idle_ttl_seconds = idle_ttl_seconds or env_float("MCP_SESSION_IDLE_TTL_SECONDS", 900.0)
        self._discovery_ttl_seconds = discovery_ttl_seconds or env_float(
            "MCP_DISCOVERY_TTL_SECONDS", 300.0
        )
        self._cleanup_interval_seconds = cleanup_interval_seconds or env_float(
            "MCP_SESSION_CLEANUP_INTERVAL_SECONDS", 60.0
        )
        self._max_sessions = max_sessions or env_int("MCP_SESSION_MAX_SIZE", 100)
        self._closing = False
        self._closed = False

    def _run_loop(self) -> None:
        if self._loop is None:
            return
        asyncio.set_event_loop(self._loop)
        self._sessions_lock = asyncio.Lock()
        self._loop.create_task(self._janitor_loop())
        try:
            self._loop.run_forever()
        finally:
            pending = asyncio.all_tasks(self._loop)
            for task in pending:
                task.cancel()
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self._loop.close()

    @property
    def session_count(self) -> int:
        return len(self._sessions)

    def warmup(
        self,
        tool: dict[str, Any] | str,
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        timeout = timeout_seconds or mcp_connect_timeout_seconds(tool)
        return self._submit(self._warmup(normalize_mcp_tool(tool)), timeout)

    def call_tool(self, tool: dict[str, Any], args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        timeout_seconds = tool_timeout_seconds(tool)
        return self._submit(self._call_tool(tool, args, state), timeout_seconds)

    def cleanup_idle(self) -> int:
        if self._loop is None:
            return 0
        return int(self._submit(self._cleanup_idle_sessions(), 5.0))

    def close(self) -> None:
        if self._closed:
            return
        if self._loop is None:
            self._closed = True
            return
        self._closing = True
        try:
            self._submit(self._close_sessions(), 5.0)
        except Exception:
            pass
        self._closed = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def _submit(self, coro: Any, timeout_seconds: float) -> Any:
        if self._closed:
            raise ToolAdapterError("MCP client manager 已关闭")
        loop = self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout_seconds)
        except Exception:
            future.cancel()
            raise

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._run_loop, name="mcp-client-manager", daemon=True)
            self._thread.start()
            return self._loop

    async def _janitor_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(self._cleanup_interval_seconds)
            await self._cleanup_idle_sessions()

    async def _warmup(self, tool: dict[str, Any]) -> dict[str, Any]:
        session = await self._get_session(tool)
        now = self._clock()
        if (
            session.discovered_tools is not None
            and now - session.discovered_at < session.discovery_ttl_seconds
        ):
            session.last_used_at = now
            return {
                "status": "ok",
                "runner_tool_id": str(tool.get("runner_tool_id") or ""),
                "connection_id": session.connection_id,
                "tools": session.discovered_tools,
                "cached": True,
            }
        try:
            async with session.lock:
                tools = await session.client.list_tools()
        except Exception:
            await self._evict_session(session.cache_key, expected=session)
            raise
        session.discovered_tools = [str(getattr(item, "name", item)) for item in tools]
        session.discovered_at = self._clock()
        session.last_used_at = session.discovered_at
        return {
            "status": "ok",
            "runner_tool_id": str(tool.get("runner_tool_id") or ""),
            "connection_id": session.connection_id,
            "tools": session.discovered_tools,
            "cached": False,
        }

    async def _call_tool(self, tool: dict[str, Any], args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        runner_tool_id = str(tool.get("runner_tool_id") or "")
        runner_name = str(tool.get("runner_name") or tool.get("tool_id") or "")
        session = await self._get_session(tool)
        started = time.perf_counter()
        try:
            async with session.lock:
                scoped_state = {**state, "_current_tool": tool}
                payload = local_mcp_payload(args, scoped_state) if is_local_mcp_stdio(runner_tool_id) else args
                result = await session.client.call_tool(runner_name, payload)
        except Exception:
            await self._evict_session(session.cache_key, expected=session)
            raise
        session.last_used_at = self._clock()
        return {
            "mcp_tool": runner_name,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "result": mcp_result_to_jsonable(result),
        }

    async def _get_session(self, tool: dict[str, Any]) -> MCPClientSession:
        cache_key = mcp_connection_cache_key(tool)
        lock = self._sessions_lock
        if lock is None:
            raise ToolAdapterError("MCP client manager 尚未启动")
        async with lock:
            await self._cleanup_idle_sessions_locked()
            session = self._sessions.get(cache_key)
            if session is not None:
                session.last_used_at = self._clock()
                return session
            await self._evict_superseded_connection_locked(tool, cache_key)
            await self._enforce_session_limit_locked()
            try:
                from fastmcp import Client
            except ImportError as exc:
                raise ToolAdapterError("缺少 fastmcp 依赖") from exc
            client = Client(build_mcp_transport(tool))
            await client.__aenter__()
            now = self._clock()
            session = MCPClientSession(
                client=client,
                lock=asyncio.Lock(),
                cache_key=cache_key,
                connection_id=mcp_connection_id(tool),
                created_at=now,
                last_used_at=now,
                idle_ttl_seconds=mcp_idle_ttl_seconds(tool, self._idle_ttl_seconds),
                discovery_ttl_seconds=mcp_discovery_ttl_seconds(
                    tool, self._discovery_ttl_seconds
                ),
            )
            self._sessions[cache_key] = session
            return session

    async def _cleanup_idle_sessions(self) -> int:
        lock = self._sessions_lock
        if lock is None:
            return 0
        async with lock:
            return await self._cleanup_idle_sessions_locked()

    async def _cleanup_idle_sessions_locked(self) -> int:
        now = self._clock()
        expired = [
            key
            for key, session in self._sessions.items()
            if not session.lock.locked()
            and now - session.last_used_at >= session.idle_ttl_seconds
        ]
        for key in expired:
            await self._close_session_locked(key)
        return len(expired)

    async def _evict_superseded_connection_locked(
        self,
        tool: dict[str, Any],
        cache_key: str,
    ) -> None:
        connection_id = mcp_connection_id(tool)
        stale_keys = [
            key
            for key, session in self._sessions.items()
            if key != cache_key
            and session.connection_id == connection_id
            and not session.lock.locked()
        ]
        for key in stale_keys:
            await self._close_session_locked(key)

    async def _enforce_session_limit_locked(self) -> None:
        while len(self._sessions) >= self._max_sessions:
            candidates = [
                session for session in self._sessions.values() if not session.lock.locked()
            ]
            if not candidates:
                raise ToolAdapterError("MCP session cache 已满且连接均在使用")
            oldest = min(candidates, key=lambda item: item.last_used_at)
            await self._close_session_locked(oldest.cache_key)

    async def _evict_session(
        self,
        cache_key: str,
        *,
        expected: MCPClientSession | None = None,
    ) -> None:
        lock = self._sessions_lock
        if lock is None:
            return
        async with lock:
            current = self._sessions.get(cache_key)
            if expected is not None and current is not expected:
                return
            await self._close_session_locked(cache_key)

    async def _close_session_locked(self, cache_key: str) -> None:
        session = self._sessions.pop(cache_key, None)
        if session is None:
            return
        try:
            await session.client.__aexit__(None, None, None)
        except Exception:
            pass

    async def _close_sessions(self) -> None:
        lock = self._sessions_lock
        if lock is None:
            return
        async with lock:
            for cache_key in list(self._sessions):
                await self._close_session_locked(cache_key)


def tool_timeout_seconds(tool: dict[str, Any]) -> float:
    value = tool.get("timeout_seconds") or tool.get("timeout")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    timeout_ms = tool.get("timeout_ms")
    if isinstance(timeout_ms, (int, float)) and timeout_ms > 0:
        return float(timeout_ms) / 1000
    connection = tool.get("mcp_connection")
    if isinstance(connection, dict):
        config = connection.get("config")
        if isinstance(config, dict):
            call_timeout_ms = config.get("call_timeout_ms")
            if isinstance(call_timeout_ms, (int, float)) and call_timeout_ms > 0:
                return float(call_timeout_ms) / 1000
    return 30.0


async def call_mcp_tool(tool: dict[str, Any], args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    try:
        from fastmcp import Client
    except ImportError as exc:
        raise ToolAdapterError("缺少 fastmcp 依赖") from exc

    runner_tool_id = str(tool.get("runner_tool_id") or "")
    runner_name = str(tool.get("runner_name") or tool.get("tool_id") or "")
    transport = build_mcp_transport(tool)
    started = time.perf_counter()
    async with Client(transport) as client:
        payload = local_mcp_payload(args, state) if is_local_mcp_stdio(runner_tool_id) else args
        result = await client.call_tool(runner_name, payload)
    return {
        "mcp_tool": runner_name,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "result": mcp_result_to_jsonable(result),
    }


mcp_client_manager = MCPClientManager()
atexit.register(mcp_client_manager.close)


def build_mcp_transport(tool_or_runner_id: dict[str, Any] | str) -> Any:
    tool = normalize_mcp_tool(tool_or_runner_id)
    connection = tool.get("mcp_connection")
    if isinstance(connection, dict):
        return build_structured_mcp_transport(connection)
    runner_tool_id = str(tool.get("runner_tool_id") or "")
    try:
        from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
    except ImportError:
        # FastMCP 的 Client 也接受 URL 字符串或 command 列表；保留轻量回退。
        if runner_tool_id.startswith("mcp+http://") or runner_tool_id.startswith("mcp+https://"):
            return runner_tool_id.removeprefix("mcp+")
        return parse_stdio_command(runner_tool_id)

    if runner_tool_id.startswith("mcp+http://") or runner_tool_id.startswith("mcp+https://"):
        url = runner_tool_id.removeprefix("mcp+")
        headers = bearer_headers(url)
        return StreamableHttpTransport(
            url=url,
            headers=headers or None,
            httpx_client_factory=mcp_http_client_factory,
        )
    command, args, cwd, env = parse_stdio_parts(runner_tool_id)
    return StdioTransport(command=command, args=args, cwd=cwd, env=env or None)


def build_structured_mcp_transport(connection: dict[str, Any]) -> Any:
    try:
        from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
    except ImportError as exc:
        raise ToolAdapterError("结构化 MCP 连接需要 fastmcp") from exc
    config = connection.get("config")
    if not isinstance(config, dict):
        raise ToolAdapterError("MCP connection config 缺失")
    transport = str(connection.get("transport") or "")
    if transport == "streamable_http":
        url = str(config.get("url") or "")
        if not url:
            raise ToolAdapterError("MCP HTTP URL 缺失")
        headers = {
            str(key): str(value)
            for key, value in (config.get("headers") or {}).items()
        }
        bearer_env = config.get("bearer_env")
        token = os.environ.get(str(bearer_env)) if bearer_env else None
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return StreamableHttpTransport(
            url=url,
            headers=headers or None,
            httpx_client_factory=mcp_http_client_factory,
        )
    if transport != "stdio":
        raise ToolAdapterError(f"不支持的 MCP transport: {transport}")
    command = str(config.get("command") or "")
    if not command:
        raise ToolAdapterError("MCP stdio command 缺失")
    args = [str(value) for value in config.get("args") or []]
    cwd = str(config["cwd"]) if config.get("cwd") else None
    env = {
        name: os.environ[name]
        for name in (str(value) for value in config.get("env_vars") or [])
        if name in os.environ
    }
    return StdioTransport(command=command, args=args, cwd=cwd, env=env or None)


def mcp_http_client_factory(**kwargs: Any) -> Any:
    import httpx

    return httpx.AsyncClient(**kwargs, trust_env=False)


def local_mcp_stdio_runner_tool_id() -> str:
    command = quote(sys.executable, safe="/")
    return (
        f"mcp+stdio://{command}"
        "?arg=-m"
        "&arg=aegora_runtime.local_mcp_server"
        "&env=PYTHONPATH,APP_ENV,APP_PRODUCT_ID,PG_HOST,PG_PORT,PG_DATABASE,PG_USER,PG_PASSWORD,PG_SSLMODE,"
        "LLM_GATEWAY_API_KEY,LLM_GATEWAY_BASE_URL,LLM_GATEWAY_CHAT_MODEL,LLM_GATEWAY_FAST_MODEL,"
        "EMBEDDING_PROVIDER,EMBEDDING_API_KEY,EMBEDDING_BASE_URL,EMBEDDING_MODEL"
        "&local=1"
    )


def is_local_mcp_stdio(runner_tool_id: str) -> bool:
    parsed = urlparse(runner_tool_id)
    return runner_tool_id.startswith("mcp+stdio://") and parse_qs(parsed.query).get("local") == ["1"]


def local_mcp_payload(args: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    return {
        "payload": {
            "args": args,
            "state": serializable_tool_state(state),
        }
    }


def serializable_tool_state(state: dict[str, Any]) -> dict[str, Any]:
    request = state.get("request")
    return {
        "request": {
            "query": getattr(request, "query", None),
            "product_id": getattr(request, "product_id", None),
            "domain_hint": getattr(request, "domain_hint", None),
            "user_id": getattr(request, "user_id", None),
            "session_id": getattr(request, "session_id", None),
            "trace_id": getattr(request, "trace_id", None),
            "history": getattr(request, "history", None),
        },
        "context": state.get("context") or {},
        "skills": state.get("skills") or [],
        "retrieved_faqs": state.get("retrieved_faqs") or [],
        "tool_observations": state.get("tool_observations") or [],
        "trace_id": state.get("trace_id"),
        "current_tool": state.get("_current_tool") if isinstance(state.get("_current_tool"), dict) else {},
    }


def bearer_headers(url: str) -> dict[str, str]:
    query = parse_qs(urlparse(url).query)
    env_names = query.get("bearer_env") or []
    if not env_names:
        return {}
    token = os.environ.get(env_names[0])
    return {"Authorization": f"Bearer {token}"} if token else {}


def parse_stdio_command(runner_tool_id: str) -> list[str]:
    command, args, _cwd, _env = parse_stdio_parts(runner_tool_id)
    return [command, *args]


def parse_stdio_parts(runner_tool_id: str) -> tuple[str, list[str], str | None, dict[str, str]]:
    parsed = urlparse(runner_tool_id)
    command = unquote(parsed.netloc or parsed.path)
    if not command:
        raise ToolAdapterError("mcp stdio command 缺失")
    query = parse_qs(parsed.query)
    args = [unquote(item) for item in query.get("arg", [])]
    cwd = query.get("cwd", [None])[0]
    env: dict[str, str] = {}
    for name in query.get("env", [""])[0].split(","):
        key = name.strip()
        if key and key in os.environ:
            env[key] = os.environ[key]
    return command, args, cwd, env


def mcp_result_to_jsonable(result: Any) -> Any:
    if hasattr(result, "data") and result.data is not None:
        return mcp_result_to_jsonable(result.data)
    if hasattr(result, "structured_content") and result.structured_content is not None:
        return mcp_result_to_jsonable(result.structured_content)
    if hasattr(result, "structuredContent") and result.structuredContent is not None:
        return mcp_result_to_jsonable(result.structuredContent)
    if hasattr(result, "content"):
        parsed_content = mcp_content_to_jsonable(result.content)
        if parsed_content is not None:
            return parsed_content
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, (str, int, float, bool)) or result is None:
        return result
    if isinstance(result, list):
        return [mcp_result_to_jsonable(item) for item in result]
    if isinstance(result, dict):
        return {str(key): mcp_result_to_jsonable(value) for key, value in result.items()}
    return str(result)


def mcp_content_to_jsonable(content: Any) -> Any:
    if not isinstance(content, list) or not content:
        return None
    values = [mcp_content_item_to_jsonable(item) for item in content]
    return values[0] if len(values) == 1 else values


def mcp_content_item_to_jsonable(item: Any) -> Any:
    if hasattr(item, "text"):
        return parse_mcp_text(str(item.text))
    if isinstance(item, dict) and "text" in item:
        return parse_mcp_text(str(item["text"]))
    return mcp_result_to_jsonable(item)


def parse_mcp_text(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return text
