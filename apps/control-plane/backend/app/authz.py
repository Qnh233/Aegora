Scope = dict[str, list[str]]


def normalize_scope(scope: dict[str, object] | None) -> Scope:
    normalized: Scope = {}
    if not scope:
        return normalized
    for key, values in scope.items():
        if not isinstance(key, str) or not isinstance(values, list):
            raise ValueError("scope 只支持 dict[str, list[str]]")
        clean_values = []
        for value in values:
            if not isinstance(value, str):
                raise ValueError("scope 只支持字符串列表")
            clean_values.append(value)
        normalized[key] = sorted(set(clean_values))
    return normalized


def merge_tool_scopes(rows: list[tuple[str, dict[str, object]]]) -> dict[str, Scope]:
    merged: dict[str, dict[str, set[str]]] = {}
    for tool_id, raw_scope in rows:
        scope = normalize_scope(raw_scope)
        tool_scope = merged.setdefault(tool_id, {})
        for scope_key, values in scope.items():
            tool_scope.setdefault(scope_key, set()).update(values)
    return {
        tool_id: {key: sorted(values) for key, values in sorted(scope.items())}
        for tool_id, scope in sorted(merged.items())
    }


def scope_is_subset(requested: dict[str, object] | None, allowed: dict[str, object] | None) -> bool:
    requested_scope = normalize_scope(requested)
    allowed_scope = normalize_scope(allowed)
    for key, requested_values in requested_scope.items():
        allowed_values = set(allowed_scope.get(key, []))
        if not set(requested_values).issubset(allowed_values):
            return False
    return True


def validate_tool_scope_subset(
    tool_id: str, requested: dict[str, object] | None, allowed: dict[str, object] | None
) -> None:
    if not scope_is_subset(requested, allowed):
        keys = ", ".join(sorted(normalize_scope(requested))) or "scope"
        raise PermissionError(f"{tool_id} 工具 scope 超出角色授权范围: {keys}")


def validate_configured_tools(
    configured_tools: set[str], current_capabilities: set[str]
) -> None:
    missing = configured_tools - current_capabilities
    if missing:
        raise PermissionError(f"缺少工具权限: {', '.join(sorted(missing))}")


def effective_tools(
    configured_tools: set[str],
    created_at_capabilities: set[str],
    current_capabilities: set[str],
) -> set[str]:
    return configured_tools & created_at_capabilities & current_capabilities
