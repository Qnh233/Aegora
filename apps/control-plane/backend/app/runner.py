import json
from openai import OpenAI
from urllib import error, request
from urllib.parse import quote

from .config import get_config
from .models import AgentRunResponse, ToolCallTrace
from .tools import execute_tool

APPROVAL_PENDING_STATUSES = {
    "pending_approval",
    "approval_pending",
    "waiting_approval",
    "requires_approval",
}


class GatewayRunnerError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Gateway Runner HTTP {status_code}: {detail}")


def call_llm_messages(messages: list[dict[str, str]], model: str | None = None) -> str:
    config = get_config()
    selected_model = model or config.llm_model
    if not config.llm_api_key or not selected_model:
        raise RuntimeError("LLM_API_KEY 或 LLM_MODEL 未配置")

    options = {"api_key": config.llm_api_key}
    if config.llm_base_url:
        options["base_url"] = config.llm_base_url
    completion = OpenAI(**options).chat.completions.create(
        model=selected_model,
        messages=messages,
    )
    answer = completion.choices[0].message.content
    if not answer or not answer.strip():
        raise RuntimeError("模型未返回文本")
    return answer.strip()


def call_llm(message: str) -> str:
    return call_llm_messages([{"role": "user", "content": message}])


def build_runner_messages(
    agent: dict[str, object],
    message: str,
    allowed_tools: set[str],
    tool_calls: list[ToolCallTrace],
) -> list[dict[str, str]]:
    system_prompt = str(agent.get("system_prompt") or "你是一个内部助手。").strip()
    messages = [{"role": "system", "content": system_prompt}]
    if allowed_tools:
        messages.append(
            {"role": "system", "content": f"可用工具: {', '.join(sorted(allowed_tools))}"}
        )
    messages.append({"role": "user", "content": message})
    if tool_calls:
        trace = "\n".join(
            f"{call.tool_id} [{call.status}]: {call.result}" for call in tool_calls
        )
        messages.append({"role": "user", "content": f"工具调用结果:\n{trace}"})
    return messages


def run_agent_once(
    agent: dict[str, object],
    message: str,
    allowed_tools: set[str],
    requested_tools: list[str],
) -> tuple[str, list[ToolCallTrace]]:
    tool_calls: list[ToolCallTrace] = []
    for tool_id in requested_tools:
        try:
            result = execute_tool(tool_id, message)
            trace = ToolCallTrace(tool_id=tool_id, status="succeeded", result=result)
        except Exception as error:
            trace = ToolCallTrace(tool_id=tool_id, status="failed", result=str(error))
        tool_calls.append(trace)

    answer = call_llm_messages(
        build_runner_messages(agent, message, allowed_tools, tool_calls),
        model=agent.get("model") if isinstance(agent.get("model"), str) else None,
    )
    return answer, tool_calls


def gateway_runs_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/v1/gateway/runs"


def gateway_approval_decision_url(base_url: str, approval_id: str) -> str:
    escaped_id = quote(approval_id, safe="")
    return f"{base_url.rstrip('/')}/v1/gateway/approvals/{escaped_id}/decide"


def parse_gateway_tool_calls(raw_calls: object) -> list[ToolCallTrace]:
    if not isinstance(raw_calls, list):
        return []
    traces: list[ToolCallTrace] = []
    for index, item in enumerate(raw_calls):
        if not isinstance(item, dict):
            traces.append(
                ToolCallTrace(tool_id=f"tool_{index}", status="unknown", result=str(item))
            )
            continue
        result = item.get("result")
        if result is None:
            result = item.get("output")
        if not isinstance(result, str):
            result = json.dumps(result, ensure_ascii=False) if result is not None else ""
        traces.append(
            ToolCallTrace(
                tool_id=str(
                    item.get("tool_id")
                    or item.get("tool_name")
                    or item.get("name")
                    or f"tool_{index}"
                ),
                status=str(item.get("status") or "unknown"),
                result=result,
            )
        )
    return traces


def parse_gateway_approval_requests(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    requests: list[dict[str, object]] = []
    for container in gateway_response_containers(payload):
        requests.extend(dict_items(container.get("approval_requests")))
        requests.extend(dict_items(container.get("approvals")))
        requests.extend(dict_items(container.get("pending_approvals")))
    return dedupe_approval_requests(requests)


def parse_gateway_tool_approval_requests(payload: object) -> list[dict[str, object]]:
    if not isinstance(payload, dict):
        return []
    requests: list[dict[str, object]] = []
    for container in gateway_response_containers(payload):
        requests.extend(approval_requests_from_tool_calls(container.get("tool_calls")))
        requests.extend(approval_requests_from_tool_calls(container.get("tool_observations")))
    requests = dedupe_approval_requests(requests)
    # 工具轨迹可能包含历史 pending 记录；fallback 只取最后一个当前候选。
    return requests[-1:] if requests else []


def gateway_response_containers(payload: dict[str, object]) -> list[dict[str, object]]:
    containers = [payload]
    for key in ("data", "result", "output", "trace"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def dict_items(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def decode_json_mapping(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def approval_requests_from_tool_calls(raw_calls: object) -> list[dict[str, object]]:
    if not isinstance(raw_calls, list):
        return []
    requests: list[dict[str, object]] = []
    for item in raw_calls:
        if not isinstance(item, dict):
            continue
        requests.extend(dict_items(item.get("approval_request")))
        requests.extend(dict_items(item.get("approval_requests")))
        nested = decode_json_mapping(item.get("result")) or decode_json_mapping(item.get("output"))
        if nested:
            requests.extend(dict_items(nested.get("approval_request")))
            requests.extend(dict_items(nested.get("approval_requests")))
        request = approval_request_from_tool_call(item, nested)
        if request:
            requests.append(request)
    return requests


def approval_request_from_tool_call(
    item: dict[str, object], nested: dict[str, object] | None
) -> dict[str, object] | None:
    status = str(item.get("status") or "").strip()
    if status not in APPROVAL_PENDING_STATUSES and not item.get("approval_id"):
        return None

    request: dict[str, object] = {}
    if nested:
        request.update(
            {
                key: value
                for key, value in nested.items()
                if key not in {"approval_request", "approval_requests"}
            }
        )
    for key in ("approval_id", "id", "tool_id", "tool_name", "name", "reason", "summary", "arguments", "input", "payload"):
        if item.get(key) is not None and request.get(key) is None:
            request[key] = item[key]
    if request.get("tool_id") is None and item.get("name") is not None:
        request["tool_id"] = item["name"]
    if request.get("approval_id") is None and nested:
        nested_id = nested.get("approval_id") or nested.get("id")
        if nested_id is not None:
            request["approval_id"] = nested_id
    return request or None


def dedupe_approval_requests(
    requests: list[dict[str, object]]
) -> list[dict[str, object]]:
    seen: set[str] = set()
    unique: list[dict[str, object]] = []
    for request in requests:
        key = str(request.get("approval_id") or request.get("id") or id(request))
        if key in seen:
            continue
        seen.add(key)
        unique.append(request)
    return unique


def parse_gateway_status(payload: object) -> str:
    if isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status.strip():
            return status.strip()
    return "succeeded"


def parse_gateway_answer(payload: object) -> str:
    if isinstance(payload, str):
        answer = payload.strip()
        if answer:
            return answer
    if not isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False)

    candidates: list[object] = [
        payload.get("answer"),
        payload.get("content"),
        payload.get("message"),
    ]
    for key in ("output", "result", "data"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.extend([nested.get("answer"), nested.get("content"), nested.get("message")])
        else:
            candidates.append(nested)

    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return json.dumps(payload, ensure_ascii=False)


def call_gateway_endpoint(url: str, payload: dict[str, object]) -> dict[str, object]:
    config = get_config()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(
            http_request, timeout=config.runner_gateway_timeout_seconds
        ) as response:
            text = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = decode_gateway_error_detail(exc.read().decode("utf-8", errors="replace"))
        raise GatewayRunnerError(exc.code, detail) from exc
    except error.URLError as exc:
        raise RuntimeError(f"Gateway Runner 不可达: {exc.reason}") from exc

    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"answer": text}
    return parsed if isinstance(parsed, dict) else {"answer": parsed}


def decode_gateway_error_detail(raw: str) -> str:
    text = raw.strip()
    if not text:
        return "Gateway Runner 请求失败"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
        if detail is not None:
            return json.dumps(detail, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False)


def call_gateway_runner(payload: dict[str, object]) -> dict[str, object]:
    config = get_config()
    if not config.runner_gateway_url:
        raise RuntimeError("RUNNER_GATEWAY_URL 未配置")
    return call_gateway_endpoint(gateway_runs_url(config.runner_gateway_url), payload)


def parse_gateway_response(
    response: dict[str, object], fallback_run_id: str = ""
) -> AgentRunResponse:
    raw_tool_calls = response.get("tool_calls")
    if raw_tool_calls is None:
        raw_tool_calls = response.get("tool_observations")
    if raw_tool_calls is None and isinstance(response.get("trace"), dict):
        raw_tool_calls = response["trace"].get("tool_calls")
    run_id = response.get("run_id")
    status = parse_gateway_status(response)
    approval_requests = parse_gateway_approval_requests(response)
    if not approval_requests and status in APPROVAL_PENDING_STATUSES:
        approval_requests = parse_gateway_tool_approval_requests(response)
    if status in APPROVAL_PENDING_STATUSES:
        status = "pending_approval"
    elif status == "succeeded" and approval_requests:
        status = "pending_approval"
    answer = parse_gateway_answer(response)
    if status == "pending_approval" and not any(
        isinstance(response.get(key), str) and response.get(key).strip()
        for key in ("answer", "content", "message")
    ):
        answer = "等待人工审批"
    return AgentRunResponse(
        run_id=str(run_id or fallback_run_id),
        answer=answer,
        tool_calls=parse_gateway_tool_calls(raw_tool_calls),
        status=status,
        approval_requests=approval_requests,
    )


def run_gateway_once(
    payload: dict[str, object], fallback_run_id: str = ""
) -> AgentRunResponse:
    return parse_gateway_response(call_gateway_runner(payload), fallback_run_id)


def decide_gateway_approval(
    approval_id: str, payload: dict[str, object]
) -> AgentRunResponse:
    config = get_config()
    if not config.runner_gateway_url:
        raise RuntimeError("RUNNER_GATEWAY_URL 未配置")
    response = call_gateway_endpoint(
        gateway_approval_decision_url(config.runner_gateway_url, approval_id), payload
    )
    return parse_gateway_response(response)
