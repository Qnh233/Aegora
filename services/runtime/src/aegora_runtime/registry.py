from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from aegora_runtime.tools import ToolMetadata


LocalHandler = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
SkillHandler = Callable[[], dict[str, Any]]


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalTool:
    tool_id: str
    runner_tool_id: str
    description: str
    handler: LocalHandler
    input_schema: dict[str, type | tuple[type, ...]]
    max_retries: int
    retry_delay_seconds: float
    metadata: ToolMetadata


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    name: str
    description: str = ""
    tags: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    handler: SkillHandler | None = None


class RunnerRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, LocalTool] = {}
        self._skills: dict[str, SkillDefinition] = {}

    def tool(
        self,
        *,
        tool_id: str,
        runner_tool_id: str | None = None,
        description: str = "",
        input_schema: dict[str, type | tuple[type, ...]] | None = None,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.0,
        metadata: ToolMetadata | None = None,
    ) -> Callable[[LocalHandler], LocalHandler]:
        def decorator(func: LocalHandler) -> LocalHandler:
            resolved_runner_tool_id = runner_tool_id or f"local.{tool_id}"
            if resolved_runner_tool_id in self._tools:
                raise ValueError(f"local tool already registered: {resolved_runner_tool_id}")
            self._tools[resolved_runner_tool_id] = LocalTool(
                tool_id=tool_id,
                runner_tool_id=resolved_runner_tool_id,
                description=description,
                handler=func,
                input_schema=input_schema or {},
                max_retries=max_retries,
                retry_delay_seconds=retry_delay_seconds,
                metadata=metadata or ToolMetadata(),
            )
            return func

        return decorator

    def skill(
        self,
        *,
        skill_id: str,
        name: str,
        description: str = "",
        tags: list[str] | tuple[str, ...] = (),
        metadata: dict[str, Any] | None = None,
    ) -> Callable[[SkillHandler], SkillHandler]:
        def decorator(func: SkillHandler) -> SkillHandler:
            if skill_id in self._skills:
                raise ValueError(f"skill already registered: {skill_id}")
            self._skills[skill_id] = SkillDefinition(
                skill_id=skill_id,
                name=name,
                description=description,
                tags=tuple(tags),
                metadata=metadata or {},
                handler=func,
            )
            return func

        return decorator

    def get_tool(self, runner_tool_id: str) -> LocalTool:
        try:
            return self._tools[runner_tool_id]
        except KeyError as exc:
            raise RegistryError(f"local tool not registered: {runner_tool_id}") from exc

    def has_tool(self, runner_tool_id: str) -> bool:
        return runner_tool_id in self._tools

    def tools(self) -> list[LocalTool]:
        return [self._tools[key] for key in sorted(self._tools)]

    def skills(self) -> list[SkillDefinition]:
        return [self._skills[key] for key in sorted(self._skills)]


registry = RunnerRegistry()
