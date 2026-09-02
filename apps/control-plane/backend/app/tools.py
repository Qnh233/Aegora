import ast
import json
import operator
import re
from datetime import datetime

from .authz import normalize_scope
from .models import ToolDefinition


LOCAL_TOOLS = [
    ToolDefinition(
        tool_id="calculator",
        name="Calculator",
        description="执行安全的基础算术表达式。",
        runner_tool_id="local.calculator",
        version="local-v1",
        read_only=True,
        idempotent=True,
        parallel_safe=True,
        requires_approval=False,
        side_effect_level="none",
        data_sensitivity="internal",
        network_access="none",
        layer="runtime",
        category="utility",
        namespace="platform",
        group_id="platform.local",
        group_name="平台本地工具",
        input_schema={
            "type": "object",
            "properties": {"expression": {"type": "string"}},
            "required": ["expression"],
        },
        scope_schema={"actions": ["calculate"]},
        scope_descriptions={"actions": "允许的计算动作。"},
    ),
    ToolDefinition(
        tool_id="time_now",
        name="Current Time",
        description="返回服务端当前时间。",
        runner_tool_id="local.time_now",
        version="local-v1",
        read_only=True,
        idempotent=False,
        parallel_safe=True,
        requires_approval=False,
        side_effect_level="none",
        data_sensitivity="internal",
        network_access="none",
        layer="runtime",
        category="utility",
        namespace="platform",
        group_id="platform.local",
        group_name="平台本地工具",
        input_schema={"type": "object", "properties": {}},
        scope_schema={"actions": ["read"]},
        scope_descriptions={"actions": "允许读取服务端当前时间。"},
    ),
    ToolDefinition(
        tool_id="text_stats",
        name="Text Stats",
        description="统计文本字符数和 token 数。",
        runner_tool_id="local.text_stats",
        version="local-v1",
        read_only=True,
        idempotent=True,
        parallel_safe=True,
        requires_approval=False,
        side_effect_level="none",
        data_sensitivity="internal",
        network_access="none",
        layer="runtime",
        category="utility",
        namespace="platform",
        group_id="platform.local",
        group_name="平台本地工具",
        input_schema={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        scope_schema={"actions": ["read"]},
        scope_descriptions={"actions": "允许读取并统计输入文本。"},
    ),
]

TOOL_SCOPE_SCHEMAS: dict[str, dict[str, list[str]]] = {
    tool.tool_id: tool.scope_schema for tool in LOCAL_TOOLS
}


def validate_tool_scope(
    tool_id: str,
    scope: dict[str, object] | None,
    scope_schema: dict[str, list[str]] | None,
) -> dict[str, list[str]]:
    normalized = normalize_scope(scope)
    if scope_schema is None:
        raise ValueError(f"工具 {tool_id} 未声明 scope schema")
    if not scope_schema:
        if normalized:
            raise ValueError(f"{tool_id} 工具 scope 不合法")
        return {}
    if not normalized:
        raise ValueError(f"{tool_id} 工具 scope 不可为空")
    for key, values in normalized.items():
        permitted_values = set(scope_schema.get(key, []))
        if permitted_values is None or not set(values).issubset(permitted_values):
            raise ValueError(f"{tool_id} 工具 scope 不合法: {key}")
    return normalized


def tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", text.lower()))


CALCULATOR_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def eval_math(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return eval_math(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.BinOp) and type(node.op) in CALCULATOR_OPS:
        left = eval_math(node.left)
        right = eval_math(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 10:
            raise ValueError("指数过大")
        return CALCULATOR_OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in CALCULATOR_OPS:
        return CALCULATOR_OPS[type(node.op)](eval_math(node.operand))
    raise ValueError("只支持数字和基础四则运算")


def calculator(message: str) -> str:
    expression = message.strip()
    if not re.fullmatch(r"[0-9+\-*/%().\s]+", expression):
        raise ValueError("请只输入算术表达式")
    result = eval_math(ast.parse(expression, mode="eval"))
    return json.dumps({"expression": expression, "result": result}, ensure_ascii=False)


def text_stats(message: str) -> str:
    parts = tokens(message)
    return json.dumps(
        {
            "characters": len(message),
            "non_space_characters": len(re.sub(r"\s+", "", message)),
            "tokens": len(parts),
        },
        ensure_ascii=False,
    )


def execute_tool(tool_id: str, message: str) -> str:
    if tool_id == "time_now":
        return json.dumps({"now": datetime.now().astimezone().isoformat()}, ensure_ascii=False)
    if tool_id == "calculator":
        return calculator(message)
    if tool_id == "text_stats":
        return text_stats(message)
    raise RuntimeError(f"工具 {tool_id} 尚未接入")
