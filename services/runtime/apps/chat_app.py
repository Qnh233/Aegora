#!/usr/bin/env python3
from __future__ import annotations

import logging
import os

for _key in ("NO_PROXY", "no_proxy"):
    _val = os.environ.get(_key)
    if _val and "::1" in _val:
        _fixed = ",".join(
            e.strip() for e in _val.split(",") if e.strip() not in ("::1", "::1/128")
        )
        os.environ[_key] = _fixed

import socket
import time
from typing import Any

import gradio as gr

from aegora_runtime.agent_loop import AgentRequest, run_agent
from aegora_runtime.config import load_settings
from aegora_runtime.logging import get_logger, log_event, setup_logging
from aegora_runtime.real_agent import build_real_dependencies
from aegora_runtime.sessions import (
    derive_user_id_from_session,
    load_recent_conversation,
    normalize_session_id,
    save_chat_turn,
    save_message_feedback,
    session_execution_lock,
)


SETTINGS = load_settings()
DEPENDENCIES = build_real_dependencies(SETTINGS)
setup_logging(SETTINGS, log_dir=SETTINGS.observability.log_dir)
LOGGER = get_logger("startup")

CSS = """
.gradio-container { max-width: 1440px !important; }
#status-row { border-bottom: 1px solid #d9dee7; padding-bottom: 10px; }
.panel-label { font-size: 12px; color: #5c6573; }
"""


def respond(
    message: str,
    history: list[dict[str, Any]] | None,
    session_id: str,
    assistant_message_ids: list[str | None] | None,
    request: gr.Request,
) -> Any:
    history = list(history or [])
    assistant_message_ids = list(assistant_message_ids or [])
    session_id = session_id_from_request(request, session_id)
    user_id = derive_user_id_from_session(session_id)
    started = time.perf_counter()
    pending_history = history + [{"role": "user", "content": message}]
    yield (
        pending_history,
        "",
        [],
        "正在判断是否需要检索 FAQ、调用工具或直接对话...",
        "Flow 已开始，等待后端节点返回。",
        {},
        "**状态：** `running`  **路由：** `-`  **循环：** `-`  **证据：** `-`",
        session_id,
        assistant_message_ids,
    )

    # PG is authoritative; the lock prevents concurrent turns from reading stale history.
    with session_execution_lock(SETTINGS, session_id):
        request_history, _ = load_recent_conversation(SETTINGS, session_id, limit=20)
        result = run_agent(
            AgentRequest(
                query=message,
                product_id=SETTINGS.app.product_id,
                user_id=user_id,
                session_id=session_id,
                history=request_history,
            ),
            DEPENDENCIES,
            SETTINGS,
        )
        total_ms = (time.perf_counter() - started) * 1000
        result["total_latency_ms"] = round(total_ms, 2)
        assistant_message = render_answer_with_assets(
            result.get("answer") or "未生成回答",
            result.get("answer_assets") or [],
        )
        try:
            save_chat_turn(
                SETTINGS,
                session_id=session_id,
                user_id=user_id,
                user_message=message,
                assistant_message=assistant_message,
                result=result,
            )
            result["persistence"] = {"saved": True}
        except Exception as exc:
            result["persistence"] = {"saved": False, "error": f"{type(exc).__name__}: {exc}"}

    if result["persistence"]["saved"]:
        history, assistant_message_ids = load_recent_conversation(SETTINGS, session_id, limit=20)
    else:
        history = pending_history + [{"role": "assistant", "content": assistant_message}]
        assistant_message_ids.append(None)
    evidence = result.get("retrieved_faqs") or []
    trace = {
        "trace_id": result.get("trace_id"),
        "instance_id": SETTINGS.observability.instance_id,
        "model_thinking_enabled": result.get("model_thinking_enabled"),
        "answer_assets": result.get("answer_assets") or [],
        "observability": result.get("observability") or [],
        "agent_trace": result.get("trace") or [],
        "decision": result.get("decision") or {},
        "tool_observations": result.get("tool_observations") or [],
        "self_check": result.get("self_check") or {},
        "persistence": result.get("persistence") or {},
    }
    model_summary = render_model_summary(result)
    flow_summary = render_flow_summary(result)
    status = (
        f"**状态：** `{result.get('status')}`  "
        f"**路由：** `{result.get('route')}`  "
        f"**循环：** `{result.get('loop_count')}`  "
        f"**证据：** `{len(evidence)}`  "
        f"**Session：** `{session_id}`  "
        f"**UserKey：** `{user_id}`  "
        f"**Trace：** `{result.get('trace_id')}`  "
        f"**耗时：** `{format_ms(total_ms)}`"
    )
    yield history, "", evidence, model_summary, flow_summary, trace, status, session_id, assistant_message_ids


def load_user_session(request: gr.Request) -> tuple[list, str, list, str, str, dict[str, Any], str, str, list[str]]:
    session_id = session_id_from_request(request)
    user_id = derive_user_id_from_session(session_id)
    try:
        history, assistant_message_ids = load_recent_conversation(SETTINGS, session_id, limit=20)
        status = f"**状态：** 已恢复会话  **Session：** `{session_id}`  **历史：** `{len(history)}`"
    except Exception as exc:
        history = []
        assistant_message_ids = []
        status = f"**状态：** 会话恢复失败 `{type(exc).__name__}: {exc}`  **Session：** `{session_id}`"
    return history, "", [], "等待输入。", "等待后端 Flow。", {}, status, session_id, assistant_message_ids


def clear_chat(session_id: str, request: gr.Request) -> tuple[list, str, list, str, str, dict[str, Any], str, str, list]:
    session_id = session_id_from_request(request, session_id)
    return [], "", [], "已清空当前页面显示，数据库会话记录仍保留。", "等待后端 Flow。", {}, f"**状态：** 已清空页面  **Session：** `{session_id}`", session_id, []


def handle_message_feedback(
    history: list[dict[str, Any]] | None,
    assistant_message_ids: list[str | None] | None,
    session_id: str,
    event: gr.LikeData,
    request: gr.Request,
) -> tuple[Any, str, str, str]:
    session_id = session_id_from_request(request, session_id)
    user_id = derive_user_id_from_session(session_id)
    message_id = feedback_message_id(history or [], assistant_message_ids or [], event.index)
    if message_id is None:
        return gr.update(visible=False), "", "", "反馈保存失败：未找到对应的助手消息。"
    rating = "positive" if event.liked is True or event.liked == "Like" else "negative"
    try:
        saved = save_message_feedback(
            SETTINGS,
            assistant_message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            rating=rating,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "message_feedback",
            assistant_message_id=message_id,
            trace_id=saved.get("trace_id"),
            session_id=session_id,
            user_id=user_id,
            rating=rating,
            reason=None,
        )
    except Exception as exc:
        return gr.update(visible=False), "", "", f"反馈保存失败：`{type(exc).__name__}: {exc}`"
    if rating == "negative":
        return gr.update(visible=True), "", str(message_id), "已记录不认可，请补充原因。"
    return gr.update(visible=False), "", "", "已记录认可，感谢反馈。"


def submit_feedback_reason(
    reason: str,
    assistant_message_id: str | None,
    session_id: str,
    request: gr.Request,
) -> tuple[Any, str, str, str]:
    if not assistant_message_id:
        return gr.update(visible=False), "", "", "反馈提交失败：没有待补充的反馈。"
    session_id = session_id_from_request(request, session_id)
    user_id = derive_user_id_from_session(session_id)
    try:
        saved = save_message_feedback(
            SETTINGS,
            assistant_message_id=assistant_message_id,
            session_id=session_id,
            user_id=user_id,
            rating="negative",
            reason=reason,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "message_feedback",
            assistant_message_id=assistant_message_id,
            trace_id=saved.get("trace_id"),
            session_id=session_id,
            user_id=user_id,
            rating="negative",
            reason=saved.get("reason"),
        )
    except Exception as exc:
        return gr.update(visible=True), reason, assistant_message_id, f"反馈提交失败：`{type(exc).__name__}: {exc}`"
    return gr.update(visible=False), "", "", "不认可原因已保存，感谢反馈。"


def feedback_message_id(
    history: list[dict[str, Any]],
    assistant_message_ids: list[str | None],
    event_index: int | tuple[int, int],
) -> str | None:
    index = event_index[0] if isinstance(event_index, tuple) else event_index
    if not isinstance(index, int) or index < 0 or index >= len(history):
        return None
    if history[index].get("role") != "assistant":
        return None
    assistant_position = sum(1 for item in history[: index + 1] if item.get("role") == "assistant") - 1
    if assistant_position < 0 or assistant_position >= len(assistant_message_ids):
        return None
    message_id = assistant_message_ids[assistant_position]
    return str(message_id) if message_id is not None else None


def render_model_summary(result: dict[str, Any]) -> str:
    observability = result.get("observability") or []
    intent = first_event(observability, "intent_classified")
    skill_retrieval = first_event(observability, "skill_retrieval")
    retrieval = first_event(observability, "retrieval")
    decision = result.get("decision") or {}
    self_check = result.get("self_check") or {}
    persistence = result.get("persistence") or {}

    lines = ["### 模型决策摘要", ""]
    lines.append(f"- 总耗时：`{format_ms(result.get('total_latency_ms'))}`")
    if intent:
        lines.extend(
            [
                f"- 原始问题：`{intent.get('query') or '-'}`",
                f"- 路由判断：`{intent.get('route_hint') or '-'}`",
                f"- 判断理由：{intent.get('reason') or '-'}",
                f"- 上下文条数：`{intent.get('history_count') or 0}`",
                f"- 独立检索问题：`{intent.get('standalone_queries') or [intent.get('standalone_query') or intent.get('query') or '-']}`",
            ]
        )
    else:
        lines.append("- 暂无分类事件。")

    if skill_retrieval:
        loaded_skills = skill_retrieval.get("loaded_skills") or []
        lines.extend(["", f"- 本轮加载 Skill：`{len(loaded_skills)}` 条"])
        for skill in loaded_skills:
            lines.append(
                f"  - `{skill.get('name') or '-'}`（{skill.get('title') or '-'}，"
                f"来源 `{skill.get('availability') or '-'}`，分数 `{skill.get('retrieval_score')}`）："
                f"{skill.get('content_preview') or '-'}"
            )

    if retrieval:
        skipped = "是" if retrieval.get("skipped") else "否"
        lines.extend(
            [
                "",
                f"- 是否跳过 RAG：{skipped}",
                f"- 实际检索问题：`{retrieval.get('search_queries') or [retrieval.get('search_query') or '-']}`",
                f"- 召回 FAQ：`{retrieval.get('count') or 0}` 条，ID `{retrieval.get('faq_ids') or []}`",
            ]
        )

    lines.extend(
        [
            "",
            f"- 最终动作：`{decision.get('route') or result.get('route') or '-'}`",
            f"- 动作理由：{decision.get('reason') or '-'}",
            f"- 工具调用：`{decision.get('tool_calls') or decision.get('tool_name') or '-'}`",
            f"- 自检结果：`{self_check.get('passed')}`，{self_check.get('reason') or '-'}",
            f"- 会话保存：`{persistence.get('saved')}`"
            + (f"，{persistence.get('error')}" if persistence.get("error") else ""),
        ]
    )
    return "\n".join(lines)


def render_flow_summary(result: dict[str, Any]) -> str:
    observability = result.get("observability") or []
    if not observability:
        return "暂无 Flow 事件。"

    lines = ["### 后端 Flow 流程", ""]
    if result.get("total_latency_ms") is not None:
        lines.extend([f"总耗时：`{format_ms(result.get('total_latency_ms'))}`", ""])
    for index, event in enumerate(observability, start=1):
        lines.append(f"{index}. {describe_event(event)}")
    return "\n".join(lines)


def describe_event(event: dict[str, Any]) -> str:
    kind = event.get("event")
    node = event.get("node") or "-"
    if kind == "node_start":
        return f"进入节点 `{node}`。"
    if kind == "node_end":
        return f"完成节点 `{node}`，动作 `{event.get('action') or '-'}`。"
    if kind == "intent_classified":
        return (
            f"分类完成：原问题 `{event.get('query') or '-'}`，路由 `{event.get('route_hint') or '-'}`，"
            f"独立问题 `{event.get('standalone_queries') or [event.get('standalone_query') or '-']}`。"
        )
    if kind == "skill_retrieval":
        loaded_skills = event.get("loaded_skills") or []
        skill_index = event.get("skill_index") or []
        if not loaded_skills:
            return (
                f"Skill 加载：未加载 Skill，原因 `{event.get('reason') or '-'}`，"
                f"候选索引 `{[item.get('name') for item in skill_index]}`，"
                f"耗时 `{format_ms(event.get('elapsed_ms'))}`。"
            )
        details = "；".join(
            f"`{item.get('name') or '-'}`（{item.get('title') or '-'}，分数 `{item.get('retrieval_score')}`，"
            f"来源 `{item.get('availability') or '-'}`，原因 `{item.get('injection_reason') or '-'}`）："
            f"{str(item.get('content_preview') or '-').rstrip('。；;')}"
            for item in loaded_skills
        )
        return f"Skill 加载：{details}；耗时 `{format_ms(event.get('elapsed_ms'))}`。"
    if kind == "retrieval":
        if event.get("skipped"):
            return f"检索跳过：路由 `{event.get('route_hint') or '-'}` 不需要 RAG。"
        return (
            f"执行检索：使用 `{event.get('search_queries') or [event.get('search_query') or '-']}`，"
            f"召回 `{event.get('count') or 0}` 条，FAQ ID `{event.get('faq_ids') or []}`。"
        )
    if kind == "agent_decision":
        tools = event.get("tool_calls") or event.get("tool_name")
        tool = f"，工具 `{tools}`" if tools else ""
        return f"模型决策：动作 `{event.get('route') or '-'}`，理由：{event.get('reason') or '-'}{tool}。"
    if kind == "tool_batch":
        return (
            f"工具批次：以 `{event.get('execution_mode') or '-'}` 模式执行 "
            f"`{event.get('call_count') or 0}` 个调用，状态 `{event.get('status') or '-'}`。"
        )
    if kind == "tool_call":
        metadata = event.get("tool_metadata") or {}
        if event.get("tool_name") == "load_skill":
            output = event.get("output") or {}
            skill = output.get("skill") if isinstance(output, dict) else {}
            content = str((skill or {}).get("content") or "")
            preview = content[:180] + ("..." if len(content) > 180 else "")
            return (
                f"经验加载 `{(skill or {}).get('name') or (event.get('args') or {}).get('name') or '-'}`："
                f"状态 `{event.get('status') or '-'}`，结果 `{output.get('reason') or '-'}`，"
                f"内容预览：{preview or '-'}。"
            )
        return (
            f"工具调用 `{event.get('call_id') or '-'}`：`{event.get('tool_name') or '-'}`，"
            f"状态 `{event.get('status') or '-'}`，只读 `{metadata.get('read_only')}`，"
            f"参数 `{event.get('args') or {}}`。"
        )
    if kind == "self_check":
        return f"回答自检：通过 `{event.get('passed')}`，原因：{event.get('reason') or '-'}。"
    if kind == "flow_end":
        return f"Flow 结束：状态 `{event.get('status') or '-'}`，路由 `{event.get('route') or '-'}`。"
    if kind == "node_error":
        return f"节点异常：`{node}`，{event.get('error_type') or '-'}：{event.get('error') or '-'}。"
    return f"`{kind or 'event'}`：`{node}`。"


def first_event(events: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for event in events:
        if event.get("event") == name:
            return event
    return None


def session_id_from_request(request: gr.Request | None, current_session_id: str | None = None) -> str:
    if request and request.username:
        return normalize_session_id(f"gradio:{request.username}")
    if current_session_id:
        return normalize_session_id(current_session_id)
    return normalize_session_id(os.environ.get("GRADIO_DEFAULT_SESSION_ID"))


def format_ms(value: Any) -> str:
    try:
        ms = float(value)
    except (TypeError, ValueError):
        return "-"
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def render_answer_with_assets(answer: str, assets: list[dict[str, Any]]) -> str:
    if not assets:
        return answer
    lines = [answer.rstrip(), "", "操作配图："]
    for asset in assets:
        platform = str(asset.get("platform") or "FAQ")
        url = str(asset.get("url") or "")
        if url.startswith(("https://", "http://")):
            lines.extend(["", f"**{platform}**", f"![{platform} 操作配图]({url})"])
    return "\n".join(lines)


def build_app() -> gr.Blocks:
    with gr.Blocks(css=CSS, title="Aegora Runtime Chat") as app:
        session_id = gr.State("")
        # Page-to-message mapping travels with the browser; PG validates ownership.
        assistant_message_ids = gr.JSON(value=[], visible=False)
        # Hidden browser component survives requests routed to another instance.
        feedback_target_id = gr.Textbox(value="", visible=False)

        with gr.Row(elem_id="status-row"):
            gr.Markdown("## Aegora Runtime Chat")
            status = gr.Markdown("**状态：** 等待输入")

        with gr.Row():
            with gr.Column(scale=7):
                chatbot = gr.Chatbot(
                    type="messages",
                    height=620,
                    sanitize_html=True,
                    label="对话",
                    placeholder="输入产品使用问题，当前为全文 + 向量 RRF 检索模式。",
                    feedback_options=("Like", "Dislike"),
                )
                with gr.Group(visible=False) as feedback_reason_group:
                    feedback_reason = gr.Textbox(
                        label="不认可原因",
                        placeholder="例如：答非所问、步骤不完整、信息不准确或语气不合适",
                        lines=2,
                        max_length=1000,
                    )
                    feedback_reason_submit = gr.Button("提交反馈原因", variant="primary", size="sm")
                feedback_status = gr.Markdown("")
                with gr.Row():
                    message = gr.Textbox(
                        placeholder="例如：如何设置悬浮窗？",
                        show_label=False,
                        lines=1,
                        scale=8,
                        autofocus=True,
                    )
                    send = gr.Button("发送", variant="primary", scale=1)
                    clear = gr.Button("清空", scale=1)

            with gr.Column(scale=5):
                with gr.Tabs():
                    with gr.Tab("检索证据"):
                        evidence = gr.JSON(label="Top-K FAQ")
                    with gr.Tab("Flow 观测"):
                        model_summary = gr.Markdown("等待输入。", label="模型决策摘要")
                        flow_summary = gr.Markdown("等待后端 Flow。", label="后端 Flow 流程")
                    with gr.Tab("原始事件"):
                        trace = gr.JSON(label="Raw Observability / Trace / Tools")
                    with gr.Tab("运行信息"):
                        gr.Markdown(
                            f"""
当前模式：`{SETTINGS.agent.loop_mode}`

能力栈：`DeepSeek + PG zhparser + BGE-M3 + RRF + Tools`

Embedding Provider：`{SETTINGS.embedding.provider}`

模型思考模式：`{"开启" if SETTINGS.deepseek.enable_thinking else "关闭"}`

BGE 启动预热：`{"开启" if SETTINGS.embedding.enable_startup_warmup else "关闭"}`

实例：`{SETTINGS.observability.instance_id}`

Pocoflow DB：`{"开启" if SETTINGS.observability.pocoflow_db_enabled else "关闭"}`

文件日志：`{"开启" if SETTINGS.observability.file_log_enabled else "关闭"}`

RRF：`candidate_k={SETTINGS.retrieval.candidate_k}`，
`rrf_k={SETTINGS.retrieval.rrf_k}`，
平局优先 `{SETTINGS.retrieval.rrf_tie_break_source}`
                            """
                        )

        inputs = [message, chatbot, session_id, assistant_message_ids]
        outputs = [chatbot, message, evidence, model_summary, flow_summary, trace, status, session_id, assistant_message_ids]
        send.click(respond, inputs=inputs, outputs=outputs)
        message.submit(respond, inputs=inputs, outputs=outputs)
        clear.click(clear_chat, inputs=[session_id], outputs=outputs)
        chatbot.like(
            handle_message_feedback,
            inputs=[chatbot, assistant_message_ids, session_id],
            outputs=[feedback_reason_group, feedback_reason, feedback_target_id, feedback_status],
            show_progress="hidden",
        )
        feedback_reason_submit.click(
            submit_feedback_reason,
            inputs=[feedback_reason, feedback_target_id, session_id],
            outputs=[feedback_reason_group, feedback_reason, feedback_target_id, feedback_status],
            show_progress="hidden",
        )
        app.load(load_user_session, outputs=outputs)

    return app


def gradio_auth() -> list[tuple[str, str]] | tuple[str, str] | None:
    users = parse_auth_users(os.environ.get("GRADIO_AUTH_USERS"))
    if users:
        return users
    username = os.environ.get("GRADIO_AUTH_USERNAME")
    password = os.environ.get("GRADIO_AUTH_PASSWORD")
    if username and password:
        return username, password
    return None


def parse_auth_users(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []
    users = []
    for item in value.split(","):
        if ":" not in item:
            continue
        username, password = item.split(":", 1)
        username = username.strip()
        password = password.strip()
        if username and password:
            users.append((username, password))
    return users


def warmup_bge() -> dict[str, Any]:
    if not SETTINGS.embedding.enable_startup_warmup:
        result = {"warmed": False, "reason": "startup_warmup_disabled", "elapsed_ms": 0.0}
    elif not DEPENDENCIES.warmup:
        result = {"warmed": False, "reason": "warmup_not_supported", "elapsed_ms": 0.0}
    else:
        result = DEPENDENCIES.warmup()
    log_event(LOGGER, logging.INFO, "bge_startup_warmup", **result)
    return result


def _pick_port(preferred: int = 7860, span: int = 20) -> int:
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise OSError(f"no free port in {preferred}-{preferred + span - 1}")


if __name__ == "__main__":
    os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
    os.environ.setdefault("no_proxy", "127.0.0.1,localhost")
    warmup_bge()
    preferred_port = int(os.environ.get("GRADIO_SERVER_PORT", "7860"))
    allow_port_fallback = os.environ.get("GRADIO_ALLOW_PORT_FALLBACK", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    port = _pick_port(preferred_port) if allow_port_fallback else preferred_port
    build_app().queue(api_open=False).launch(
        server_name="0.0.0.0",
        server_port=port,
        auth=gradio_auth(),
        auth_message="请输入公司内测账号后继续。",
        show_api=False,
        share=os.environ.get("GRADIO_SHARE", "false").strip().lower() in {"1", "true", "yes", "on"},
        root_path=os.environ.get("GRADIO_ROOT_PATH") or None,
    )
