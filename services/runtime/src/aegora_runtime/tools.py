from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable


ToolHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
ToolHook = Callable[[dict[str, Any]], None]


class ToolError(RuntimeError):
    pass


class ToolValidationError(ToolError):
    pass


@dataclass(frozen=True)
class ToolMetadata:
    read_only: bool = False
    parallel_safe: bool = False
    idempotent: bool = False
    side_effects: str = "unspecified"

    def as_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "parallel_safe": self.parallel_safe,
            "idempotent": self.idempotent,
            "side_effects": self.side_effects,
        }


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, type | tuple[type, ...]]
    handler: ToolHandler
    max_retries: int = 1
    retry_delay_seconds: float = 0.0
    metadata: ToolMetadata = field(default_factory=ToolMetadata)
    json_schema: dict[str, Any] | None = None


@dataclass
class ToolLifecycleHooks:
    before_call: list[ToolHook] = field(default_factory=list)
    after_call: list[ToolHook] = field(default_factory=list)
    on_error: list[ToolHook] = field(default_factory=list)

    def fire_before(self, event: dict[str, Any]) -> None:
        _fire(self.before_call, event)

    def fire_after(self, event: dict[str, Any]) -> None:
        _fire(self.after_call, event)

    def fire_error(self, event: dict[str, Any]) -> None:
        _fire(self.on_error, event)


class RuntimeToolExecutor:
    """本轮已注入工具的执行表，不负责发现或授权工具。"""

    def __init__(self, hooks: ToolLifecycleHooks | None = None):
        self._tools: dict[str, ToolSpec] = {}
        self.hooks = hooks or ToolLifecycleHooks()

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolError(f"unknown tool: {name}") from exc

    def catalog(self) -> list[dict[str, Any]]:
        items = []
        for spec in self._tools.values():
            required_args = sorted(spec.input_schema)
            if not required_args and isinstance(spec.json_schema, dict):
                required = spec.json_schema.get("required")
                if isinstance(required, list):
                    required_args = sorted(item for item in required if isinstance(item, str))
            item = {
                "name": spec.name,
                "description": spec.description,
                "required_args": required_args,
                **spec.metadata.as_dict(),
            }
            if isinstance(spec.json_schema, dict) and spec.json_schema:
                item["input_schema"] = spec.json_schema
            items.append(item)
        return items

    def run(
        self,
        name: str,
        args: dict[str, Any],
        state: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        # 真正执行入口：先按本轮 ToolSpec 白名单取工具，再调用 spec.handler。
        spec = self.get(name)
        validate_args(spec, args)

        base_event = {
            "tool_name": name,
            "args": safe_args(args),
            "trace_id": trace_id_from_state(state),
            "session_id": request_value(state, "session_id"),
            "user_id": request_value(state, "user_id"),
            "call_id": call_id,
            "tool_metadata": spec.metadata.as_dict(),
        }
        self.hooks.fire_before({**base_event, "event": "tool_before"})

        last_exc: Exception | None = None
        for attempt in range(1, spec.max_retries + 1):
            started = time.perf_counter()
            try:
                # spec.handler 由 tool_adapters 注入，最终会分发到本地函数或 MCP call_tool。
                output = spec.handler(args, state)
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                event = {
                    **base_event,
                    "event": "tool_after",
                    "attempt": attempt,
                    "latency_ms": latency_ms,
                    "status": "ok",
                    "output": output,
                }
                self.hooks.fire_after(event)
                return {
                    "call_id": call_id,
                    "tool_name": name,
                    "args": safe_args(args),
                    "output": output,
                    "status": "ok",
                    "latency_ms": latency_ms,
                    "attempt": attempt,
                    "tool_metadata": spec.metadata.as_dict(),
                }
            except Exception as exc:
                last_exc = exc
                latency_ms = round((time.perf_counter() - started) * 1000, 2)
                self.hooks.fire_error(
                    {
                        **base_event,
                        "event": "tool_error",
                        "attempt": attempt,
                        "latency_ms": latency_ms,
                        "status": "error",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                if attempt < spec.max_retries and spec.retry_delay_seconds > 0:
                    time.sleep(spec.retry_delay_seconds)

        detail = f": {last_exc}" if last_exc else ""
        raise ToolError(f"tool failed after {spec.max_retries} attempt(s): {name}{detail}") from last_exc

    def run_many(
        self,
        calls: list[dict[str, Any]],
        state: dict[str, Any],
        *,
        max_parallel: int = 4,
    ) -> dict[str, Any]:
        normalized = [normalize_tool_call(call, index) for index, call in enumerate(calls)]
        parallel = len(normalized) > 1 and all(self._parallel_eligible(call["tool_name"]) for call in normalized)
        started = time.perf_counter()
        if parallel:
            workers = max(1, min(max_parallel, len(normalized)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="agent-tool") as executor:
                results = list(executor.map(lambda call: self._run_call_safely(call, state), normalized))
        else:
            results = [self._run_call_safely(call, state) for call in normalized]
        return {
            "execution_mode": "parallel" if parallel else "sequential",
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "results": results,
        }

    def _parallel_eligible(self, name: str) -> bool:
        try:
            metadata = self.get(name).metadata
        except ToolError:
            return False
        return metadata.read_only and metadata.parallel_safe

    def _run_call_safely(self, call: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.run(
                call["tool_name"],
                call["tool_args"],
                state,
                call_id=call["call_id"],
            )
        except Exception as exc:
            return {
                "call_id": call["call_id"],
                "tool_name": call["tool_name"],
                "args": safe_args(call["tool_args"]),
                "status": "error",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }


def validate_args(spec: ToolSpec, args: dict[str, Any]) -> None:
    for key, expected_type in spec.input_schema.items():
        if key not in args:
            raise ToolValidationError(f"tool {spec.name} missing required arg: {key}")
        if not isinstance(args[key], expected_type):
            raise ToolValidationError(
                f"tool {spec.name} arg {key} expects {expected_type}, got {type(args[key]).__name__}"
            )


def safe_args(args: dict[str, Any]) -> dict[str, Any]:
    hidden = {"password", "token", "api_key", "secret"}
    return {key: "***" if key.lower() in hidden else value for key, value in args.items()}


def normalize_tool_call(call: dict[str, Any], index: int) -> dict[str, Any]:
    name = str(call.get("tool_name") or call.get("name") or "").strip()
    args = call.get("tool_args") if isinstance(call.get("tool_args"), dict) else call.get("args")
    return {
        "call_id": str(call.get("call_id") or f"tool-call-{index + 1}"),
        "tool_name": name,
        "tool_args": args if isinstance(args, dict) else {},
    }


def _fire(hooks: list[ToolHook], event: dict[str, Any]) -> None:
    for hook in hooks:
        hook(event)


def request_value(state: dict[str, Any], name: str) -> str | None:
    request = state.get("request")
    value = getattr(request, name, None)
    return str(value) if value else None


def trace_id_from_state(state: dict[str, Any]) -> str | None:
    return request_value(state, "trace_id") or request_value(state, "session_id") or request_value(state, "user_id")
