#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import logging
import signal
from typing import Any

from aegora_runtime.config import ConfigError, load_settings
from aegora_runtime.logging import get_logger, log_event, setup_logging
from aegora_runtime.real_agent import build_real_dependencies
from aegora_runtime.service import ChatTurnInput, handle_chat_turn
from aegora_runtime.sessions import (
    derive_user_id_from_session,
    find_assistant_message_by_request_metadata,
    normalize_session_id,
    save_message_feedback,
)
from aegora_runtime.wecom import (
    build_stream_feedback_id,
    build_wecom_feedback,
    build_wecom_turn,
    chunk_text,
    format_answer_for_wecom,
)


try:
    from wecom_aibot_sdk import WSClient, generate_req_id
except ImportError:  # pragma: no cover - tested through optional dependency boundary.
    WSClient = None  # type: ignore[assignment]
    generate_req_id = None  # type: ignore[assignment]


SETTINGS = load_settings()
setup_logging(SETTINGS, log_dir=SETTINGS.observability.log_dir)
LOGGER = get_logger("wecom_aibot")

WECOM_PROGRESS_EVENT_TYPES = {
    "node_start",
    "skill_retrieval",
    "intent_classified",
    "pre_guard",
    "retrieval",
    "agent_decision",
    "tool_batch",
    "tool_call",
    "self_check",
}


class SDKLogger:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message: str, *args: Any) -> None:
        self._logger.debug("%s %s", message, " ".join(str(arg) for arg in args))

    def info(self, message: str, *args: Any) -> None:
        self._logger.info("%s %s", message, " ".join(str(arg) for arg in args))

    def warn(self, message: str, *args: Any) -> None:
        self._logger.warning("%s %s", message, " ".join(str(arg) for arg in args))

    def error(self, message: str, *args: Any) -> None:
        self._logger.error("%s %s", message, " ".join(str(arg) for arg in args))


def build_client() -> Any:
    if WSClient is None:
        raise ConfigError("缺少 wecom-aibot-sdk 依赖，请安装 requirements.docker.txt 或 requirements.txt")
    if not SETTINGS.wecom_aibot.bot_id:
        raise ConfigError("APP_MODE=wecom 需要配置 WECOM_AIBOT_ID")
    if not SETTINGS.wecom_aibot.secret:
        raise ConfigError("APP_MODE=wecom 需要配置 WECOM_AIBOT_SECRET")

    return WSClient(
        bot_id=SETTINGS.wecom_aibot.bot_id,
        secret=SETTINGS.wecom_aibot.secret,
        scene=SETTINGS.wecom_aibot.scene,
        plug_version=SETTINGS.wecom_aibot.plug_version,
        reconnect_interval=SETTINGS.wecom_aibot.reconnect_interval_ms,
        max_reconnect_attempts=SETTINGS.wecom_aibot.max_reconnect_attempts,
        heartbeat_interval=SETTINGS.wecom_aibot.heartbeat_interval_ms,
        request_timeout=SETTINGS.wecom_aibot.request_timeout_ms,
        ws_url=SETTINGS.wecom_aibot.ws_url,
        logger=SDKLogger(LOGGER),
    )


async def run() -> None:
    settings = SETTINGS
    settings.validate_runtime_secrets()
    dependencies = build_real_dependencies(settings)
    client = build_client()
    stop_event = asyncio.Event()

    async def on_text(frame: dict[str, Any]) -> None:
        stream_id = generate_req_id("stream")
        try:
            turn = build_wecom_turn(frame)
            session_id = normalize_session_id(turn.session_id)
            turn.metadata["wecom_stream_id"] = stream_id
            feedback_id = build_stream_feedback_id(stream_id)
            feedback = {"id": feedback_id} if feedback_id else None
            loop = asyncio.get_running_loop()
            progress_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

            def stream_handler(event: dict[str, Any]) -> None:
                loop.call_soon_threadsafe(progress_events.put_nowait, event)

            await client.reply_stream(
                frame,
                stream_id,
                "开始处理你的问题。",
                False,
                feedback=feedback,
            )
            log_event(
                LOGGER,
                logging.INFO,
                "wecom_message_received",
                session_id=session_id,
                msgid=turn.metadata.get("msgid"),
                chatid=turn.metadata.get("chatid"),
                req_id=turn.metadata.get("ws_req_id"),
            )
            task = asyncio.create_task(asyncio.to_thread(
                handle_chat_turn,
                ChatTurnInput(
                    message=turn.message,
                    session_id=session_id,
                    product_id=settings.app.product_id,
                    source="wecom",
                    metadata=turn.metadata,
                    stream_handler=stream_handler,
                ),
                dependencies,
                settings,
            ))
            last_progress_message = ""
            while not task.done() or not progress_events.empty():
                try:
                    event = await asyncio.wait_for(progress_events.get(), timeout=0.2)
                except asyncio.TimeoutError:
                    continue
                message = wecom_progress_message(event)
                if not message or message == last_progress_message:
                    continue
                last_progress_message = message
                await client.reply_stream(frame, stream_id, message, False)

            response = await task
            answer = format_answer_for_wecom(response.get("answer") or "未生成回答", response.get("assets") or [])
            chunks = chunk_text(answer)
            for index, chunk in enumerate(chunks):
                await client.reply_stream(frame, stream_id, chunk, index == len(chunks) - 1)
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "wecom_message_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            await client.reply_stream(frame, stream_id, "抱歉，当前暂时无法处理这条消息，请稍后再试。", True)

    async def on_feedback(frame: dict[str, Any]) -> None:
        try:
            feedback = build_wecom_feedback(frame)
            session_id = normalize_session_id(feedback.session_id)
            if feedback.rating is None:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "wecom_feedback_ignored",
                    session_id=session_id,
                    assistant_message_id=feedback.assistant_message_id,
                    feedback_type=feedback.metadata.get("feedback_type"),
                    reason="unsupported_feedback_type",
                )
                return
            assistant_message_id = feedback.assistant_message_id
            if assistant_message_id is None and feedback.stream_id:
                assistant_message_id = await asyncio.to_thread(
                    find_assistant_message_by_request_metadata,
                    settings,
                    session_id=session_id,
                    key="wecom_stream_id",
                    value=feedback.stream_id,
                )
            if assistant_message_id is None:
                raise ValueError("cannot map wecom feedback to assistant message")

            saved = await asyncio.to_thread(
                save_message_feedback,
                settings,
                assistant_message_id=assistant_message_id,
                session_id=session_id,
                user_id=derive_user_id_from_session(session_id),
                rating=feedback.rating,
                reason=feedback.reason,
            )
            log_event(
                LOGGER,
                logging.INFO,
                "wecom_feedback_saved",
                session_id=session_id,
                assistant_message_id=assistant_message_id,
                rating=feedback.rating,
                feedback_id=saved.get("id"),
                trace_id=saved.get("trace_id"),
                wecom_stream_id=feedback.stream_id,
            )
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "wecom_feedback_error",
                error_type=type(exc).__name__,
                error=str(exc),
            )

    async def on_enter_chat(frame: dict[str, Any]) -> None:
        await client.reply_welcome(
            frame,
            {
                "msgtype": "text",
                "text": {"content": "您好，我是 AiCoin 客服助手。请直接发送您遇到的问题。"},
            },
        )

    client.on("authenticated", lambda: log_event(LOGGER, logging.INFO, "wecom_authenticated"))
    client.on("disconnected", lambda reason: log_event(LOGGER, logging.WARNING, "wecom_disconnected", reason=reason))
    client.on("reconnecting", lambda attempt: log_event(LOGGER, logging.WARNING, "wecom_reconnecting", attempt=attempt))
    client.on("error", lambda error: log_event(LOGGER, logging.ERROR, "wecom_sdk_error", error=str(error)))
    client.on("message.text", on_text)
    client.on("event.feedback_event", on_feedback)
    client.on("event.enter_chat", on_enter_chat)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    await client.connect()
    log_event(LOGGER, logging.INFO, "wecom_worker_started")
    try:
        await stop_event.wait()
    finally:
        log_event(LOGGER, logging.INFO, "wecom_worker_stopping")
        await client.disconnect()


def wecom_progress_message(event: dict[str, Any]) -> str | None:
    event_type = event.get("type") or (event.get("payload") or {}).get("event")
    if event_type not in WECOM_PROGRESS_EVENT_TYPES:
        return None
    message = str(event.get("message") or "").strip()
    if not message:
        return None
    return f"进度：{message}"


if __name__ == "__main__":
    asyncio.run(run())
