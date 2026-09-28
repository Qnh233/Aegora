from __future__ import annotations

from typing import Any, Protocol

from aegora_runtime.deepseek import ChatMessage
from aegora_runtime.memory.models import MemoryCandidate


MEMORY_EXTRACTION_PROTOCOL = """你是 Aegora 的长期记忆候选提取器。只输出 JSON 对象。
目标：仅从用户本轮原话中提取“明确、稳定、未来可能复用”的事实或偏好；不要推断。
允许 operation: upsert, forget。
允许 namespace: preferences, profile, work_context。
允许 memory_type: semantic, profile, episodic, summary。
evidence_type 只能是 explicit_fact、explicit_instruction 或 inferred；inferred 仅用于标记不应自动应用的候选。
规则：
1. 只提取用户明确陈述的事实/偏好，禁止从助手回答、工具结果或上下文猜测。
2. “以后都用 Python”“我更喜欢简短回答”“我现在改用 Go”可提取。
3. 一次性问题、临时数字、闲聊、推测、情绪、模型生成内容不要提取。
4. 密码、token、API key、私钥、验证码、身份证/银行卡、病史、宗教、政治立场、性取向、种族、工会、犯罪记录等敏感信息不要提取。
5. forget 只用于用户明确要求忘记/不要记住某个已有记忆；evidence_type 必须 explicit_instruction。
6. update_intent 仅当用户明确表达“现在改成/以后改用/不再/更新为”等替换旧事实时为 true。
7. episodic 记忆必须给 ttl_days，范围 1-365；稳定 semantic/profile 通常 ttl_days=null。
8. confidence 表示“这条候选确实由用户明确表达”的置信度，不是事实真假概率。
9. 最多返回 5 条候选；没有合适候选时返回空数组。
输出：
{"candidates":[{"operation":"upsert","namespace":"preferences","key":"preferred_language","value":"Python","memory_type":"semantic","evidence_type":"explicit_fact","confidence":0.99,"importance":0.8,"update_intent":false,"ttl_days":null,"rationale":"用户明确表示长期偏好"}]}
"""


class JsonMemoryExtractionClient(Protocol):
    def chat_json(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> dict[str, Any]: ...


def extract_memory_candidates(
    client: JsonMemoryExtractionClient,
    *,
    user_message: str,
    source_agent_id: str | None,
    source_session_id: str | None,
    source_trace_id: str | None,
    model: str | None = None,
) -> list[MemoryCandidate]:
    content = str(user_message or "").strip()
    if not content:
        return []
    payload = client.chat_json(
        [
            ChatMessage(role="system", content=MEMORY_EXTRACTION_PROTOCOL),
            ChatMessage(
                role="user",
                content=f"请从下面用户原话提取长期记忆候选：\n\n{content[:6000]}",
            ),
        ],
        model=model,
        temperature=0.0,
    )
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list):
        return []

    candidates: list[MemoryCandidate] = []
    for raw in raw_candidates[:5]:
        if not isinstance(raw, dict):
            continue
        candidate = parse_memory_candidate(
            raw,
            source_agent_id=source_agent_id,
            source_session_id=source_session_id,
            source_trace_id=source_trace_id,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def parse_memory_candidate(
    raw: dict[str, Any],
    *,
    source_agent_id: str | None = None,
    source_session_id: str | None = None,
    source_trace_id: str | None = None,
) -> MemoryCandidate | None:
    namespace = str(raw.get("namespace") or "").strip()
    if namespace not in {"preferences", "profile", "work_context"}:
        return None

    operation = str(raw.get("operation") or "").strip().lower()
    key = str(raw.get("key") or "").strip()[:100]
    value_raw = raw.get("value")
    value = str(value_raw).strip()[:800] if value_raw is not None else None
    rationale = str(raw.get("rationale") or "").strip()[:500] or None
    memory_type = str(raw.get("memory_type") or "semantic").strip()
    evidence_type = str(raw.get("evidence_type") or "inferred").strip()
    ttl_raw = raw.get("ttl_days")
    try:
        ttl_days = int(ttl_raw) if ttl_raw not in (None, "") else None
        confidence = float(raw.get("confidence") or 0)
        importance = float(raw.get("importance") or 0.5)
        return MemoryCandidate(
            operation=operation,
            namespace=namespace,
            key=key,
            value=value,
            memory_type=memory_type,
            evidence_type=evidence_type,
            confidence=max(0.0, min(1.0, confidence)),
            importance=max(0.0, min(1.0, importance)),
            update_intent=bool(raw.get("update_intent", False)),
            ttl_days=ttl_days,
            rationale=rationale,
            source_agent_id=source_agent_id,
            source_session_id=source_session_id,
            source_trace_id=source_trace_id,
        )
    except (TypeError, ValueError):
        return None
