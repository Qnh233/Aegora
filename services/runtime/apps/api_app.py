#!/usr/bin/env python3
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from aegora_runtime import runtime_context
from aegora_runtime.config import load_settings
from aegora_runtime.configured_runner import decide_approval, run_configured_turn
from aegora_runtime.logging import get_logger, log_event, setup_logging
from aegora_runtime.metrics import instrument_fastapi, metrics_response
from aegora_runtime.real_agent import build_real_dependencies
from aegora_runtime.runtime_approvals import ApprovalDecision, ApprovalError, default_approval_store
from aegora_runtime.runtime_invalidation import RuntimeInvalidationSubscriber, runtime_invalidation_settings
from aegora_runtime.service import ChatTurnInput, handle_chat_turn
from aegora_runtime.sessions import (
    derive_user_id_from_session,
    load_recent_conversation,
    normalize_session_id,
    save_chat_turn,
    save_message_feedback,
)
from aegora_runtime.wecom import build_wecom_turn


SETTINGS = load_settings()
DEPENDENCIES = build_real_dependencies(SETTINGS)
setup_logging(SETTINGS, log_dir=SETTINGS.observability.log_dir)
LOGGER = get_logger("api")

OPENAPI_TAGS = [
    {"name": "system", "description": "服务健康检查与基础信息。"},
    {"name": "chat", "description": "旧客服对话接口，保留给现有 AiCoin RAG 链路。"},
    {"name": "gateway", "description": "平台调试入口，按 `agent_id + release_id` 直跑指定发布版本。"},
    {"name": "webhooks", "description": "外部渠道入口，按 `agent_id + version + cid + sender_uid` 调用。"},
    {"name": "approvals", "description": "审批恢复执行接口。"},
    {"name": "sessions", "description": "会话消息与反馈查询接口。"},
    {"name": "wecom", "description": "企业微信 HTTP 接入入口。"},
]

app = FastAPI(
    title="Aegora Runtime API",
    version="0.1.0",
    description=(
        "Aegora Runtime Runner 的 HTTP API。`/v1/gateway/runs` 用于平台调试，"
        "`/v1/webhooks/runs` 用于 OA/IM/Webhook 渠道接入。"
    ),
    openapi_tags=OPENAPI_TAGS,
)
instrument_fastapi(app)

_INVALIDATION_STOP = threading.Event()
_INVALIDATION_SUBSCRIBER = RuntimeInvalidationSubscriber(runtime_invalidation_settings())
_INVALIDATION_THREAD: threading.Thread | None = None


@app.on_event("startup")
def start_runtime_invalidation_subscriber() -> None:
    global _INVALIDATION_THREAD
    if not _INVALIDATION_SUBSCRIBER.settings.enabled:
        return
    _INVALIDATION_STOP.clear()
    _INVALIDATION_THREAD = threading.Thread(
        target=_INVALIDATION_SUBSCRIBER.run,
        args=(_INVALIDATION_STOP,),
        daemon=True,
        name="aegora-runtime-invalidation",
    )
    _INVALIDATION_THREAD.start()


@app.on_event("shutdown")
def stop_runtime_invalidation_subscriber() -> None:
    _INVALIDATION_STOP.set()
    if _INVALIDATION_THREAD is not None:
        _INVALIDATION_THREAD.join(timeout=2)


SENSITIVE_LOG_KEYS = {
    "authorization",
    "api_key",
    "access_token",
    "refresh_token",
    "token",
    "secret",
    "password",
    "reply_url",
}


def redact_for_log(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[redacted]" if key.lower() in SENSITIVE_LOG_KEYS else redact_for_log(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_log(item) for item in value]
    return value


def request_body_preview(raw_body: bytes, *, limit: int = 2000) -> str:
    text = raw_body.decode("utf-8", errors="replace")
    try:
        text = json.dumps(redact_for_log(json.loads(text)), ensure_ascii=False, separators=(",", ":"))
    except json.JSONDecodeError:
        pass
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated>"


@app.exception_handler(RequestValidationError)
async def log_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    raw_body = await request.body()
    log_event(
        LOGGER,
        logging.WARNING,
        "api_request_validation_error",
        method=request.method,
        path=request.url.path,
        query=str(request.url.query),
        content_type=request.headers.get("content-type"),
        body_preview=request_body_preview(raw_body),
        validation_errors=redact_for_log(jsonable_encoder(exc.errors())),
    )
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})


@app.exception_handler(StarletteHTTPException)
async def log_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code >= 400:
        raw_body = await request.body()
        log_event(
            LOGGER,
            logging.WARNING if exc.status_code < 500 else logging.ERROR,
            "api_http_error_422" if exc.status_code == 422 else "api_http_error",
            method=request.method,
            path=request.url.path,
            query=str(request.url.query),
            status_code=exc.status_code,
            detail=redact_for_log(jsonable_encoder(exc.detail)),
            body_preview=request_body_preview(raw_body) if raw_body else "",
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": jsonable_encoder(exc.detail)},
        headers=exc.headers,
    )


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000, description="用户本轮输入消息。", examples=["我要购买会员"])
    session_id: str = Field(min_length=1, max_length=512, description="旧客服链路的会话 ID。", examples=["web:sess_123"])
    product_id: str | None = Field(default=None, max_length=128, description="产品 ID，默认取服务配置。", examples=["aicoin"])
    domain_hint: str | None = Field(default=None, max_length=128, description="可选业务域提示。", examples=["membership"])
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传元数据。")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "我要购买会员",
                "session_id": "web:sess_123",
                "product_id": "aicoin",
                "domain_hint": "membership",
                "metadata": {"channel": "im"},
            }
        }
    )


class GatewayRunRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100, description="Agent Platform 中的 Agent ID。", examples=["agent_1"])
    release_id: str = Field(min_length=1, max_length=100, description="具体发布快照 ID。该接口用于平台调试，显式指定 release。", examples=["rel_1"])
    actor_id: str = Field(min_length=1, max_length=100, description="运行时鉴权主体。Resolver 会按该用户做可见度和工具权限校验。", examples=["u_console"])
    channel: str = Field(min_length=1, max_length=100, description="调用渠道。必须在 agent/release 允许渠道中。", examples=["web_console"])
    message: str = Field(min_length=1, max_length=10000, description="用户本轮消息。", examples=["帮我查询会员权益"])
    session_id: str | None = Field(default=None, max_length=512, description="可选会话窗口 ID。未传时自动生成。", examples=["debug-session"])
    reply_url: str | None = Field(default=None, max_length=2048, description="可选异步回调地址。同步返回不受回调成功与否影响。", examples=["https://example.com/replies/token"])
    cid: str | None = Field(default=None, max_length=512, description="可选外部渠道会话 ID，仅用于回调透传。", examples=["chat_123"])
    event_id: str | None = Field(default=None, max_length=512, description="可选事件 ID，仅用于回调透传。", examples=["evt_1"])
    mid: str | None = Field(default=None, max_length=512, description="可选消息 ID，仅用于回调透传。", examples=["msg_1"])
    sender_uid: str | None = Field(default=None, max_length=512, description="可选发言人 ID。传入时用于写入 chat_messages.user_id。", examples=["oa_user_1"])
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传元数据。")
    stream: bool = Field(default=False, description="预留字段，当前必须为 false。")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "agent_id": "agent_1",
                "release_id": "rel_1",
                "actor_id": "u_console",
                "channel": "web_console",
                "message": "帮我查询会员权益",
                "session_id": "debug-session",
                "metadata": {"source": "console"},
            }
        }
    )


class WebhookRunRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=100, description="Agent Platform 中的 Agent ID。", examples=["agent_1"])
    version: int = Field(ge=1, le=1000000, description="对外可读的发布版本号，Runner 会在运行时解析到真实 release_id。", examples=[3])
    channel: str = Field(min_length=1, max_length=100, description="渠道类型，例如 `oa`、`im`。", examples=["oa"])
    cid: str = Field(min_length=1, max_length=512, description="外部会话窗口或群聊 ID。Runner 内部直接作为 `session_id`。", examples=["chat_123"])
    sender_uid: str = Field(
        validation_alias=AliasChoices("sender_uid", "sender_user"),
        min_length=1,
        max_length=512,
        description="当前发言人 UID。首期同时作为运行时 `actor_id` 和 `user_id`。`sender_user` 仅兼容旧调用。",
        examples=["oa_user_1"],
    )
    message: str = Field(min_length=1, max_length=10000, description="用户本轮消息。", examples=["帮我查询会员权益"])
    reply_url: str | None = Field(default=None, max_length=2048, description="可选异步回调地址。", examples=["https://example.com/replies/webhook-token"])
    event_id: str | None = Field(default=None, max_length=512, description="可选原始事件 ID。", examples=["evt_2"])
    mid: str | None = Field(default=None, max_length=512, description="可选原始消息 ID。", examples=["msg_2"])
    metadata: dict[str, Any] = Field(default_factory=dict, description="透传元数据。")
    stream: bool = Field(default=False, description="预留字段，当前必须为 false。")

    model_config = ConfigDict(
        populate_by_name=True,
        json_schema_extra={
            "example": {
                "agent_id": "agent_1",
                "version": 3,
                "channel": "oa",
                "cid": "chat_123",
                "sender_uid": "oa_user_1",
                "message": "帮我查询会员权益",
                "event_id": "evt_2",
                "mid": "msg_2",
            }
        }
    )


class ApprovalDecisionRequest(BaseModel):
    decision: str = Field(pattern="^(approved|rejected)$", description="审批结果。", examples=["approved"])
    decided_by: str | None = Field(default=None, min_length=1, max_length=100, description="审批人 ID。优先于 actor_id。", examples=["u_reviewer"])
    actor_id: str | None = Field(default=None, min_length=1, max_length=100, description="兼容字段，未传 decided_by 时可用。", examples=["u_console"])
    comment: str | None = Field(default=None, max_length=1000, description="审批备注。", examples=["同意执行"])

    def reviewer_id(self) -> str:
        reviewer = self.decided_by or self.actor_id
        if not reviewer:
            raise ValueError("decided_by 或 actor_id 必填")
        return reviewer


class FeedbackRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=512, description="会话 ID。", examples=["s1"])
    rating: str = Field(description="反馈结果，只接受 `positive` 或 `negative`。", examples=["positive"])
    reason: str | None = Field(default=None, max_length=1000, description="可选反馈原因。", examples=["回答准确"])


class WeComSender(BaseModel):
    userid: str = Field(min_length=1, max_length=512)


class WeComText(BaseModel):
    content: str = Field(default="", max_length=8000)


class WeComQuote(BaseModel):
    msgtype: str | None = Field(default=None, max_length=32)
    text: WeComText | None = None


class WeComAIBotRequest(BaseModel):
    msgid: str | None = Field(default=None, max_length=1024)
    aibotid: str | None = Field(default=None, max_length=256)
    chatid: str | None = Field(default=None, max_length=512)
    chattype: str | None = Field(default=None, max_length=32)
    sender: WeComSender = Field(alias="from")
    response_url: str | None = Field(default=None, max_length=2048)
    msgtype: str = Field(default="text", max_length=32)
    text: WeComText | None = None
    quote: WeComQuote | None = None


class RootResponse(BaseModel):
    ok: bool
    service: str


class HealthzResponse(BaseModel):
    ok: bool
    instance_id: str
    loop_mode: str
    embedding_provider: str


class ChatTurnResponse(BaseModel):
    session_id: str
    user_id: str
    assistant_message_id: str | int
    answer: str
    route: str | None = None
    status: str | None = None
    trace_id: str | None = None
    latency_ms: float | int | None = None
    loop_mode: str | None = None
    loop_count: int | None = None
    intent: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    assets: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    flow: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    model_thinking_enabled: bool | None = None


class GatewayRunResponse(BaseModel):
    run_id: str
    session_id: str
    agent_id: str
    release_id: str
    actor_id: str
    channel: str
    answer: str
    route: str | None = None
    status: str | None = None
    trace_id: str | None = None
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    approval_requests: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    flow: list[dict[str, Any]] = Field(default_factory=list)


class WebhookRunResponse(BaseModel):
    run_id: str
    session_id: str
    agent_id: str
    release_id: str | None = None
    version: int | None = None
    actor_id: str
    user_id: str
    channel: str
    answer: str
    route: str | None = None
    status: str | None = None
    trace_id: str | None = None
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    approval_requests: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    flow: list[dict[str, Any]] = Field(default_factory=list)


class ApprovalDecisionResponse(BaseModel):
    run_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    release_id: str | None = None
    actor_id: str | None = None
    channel: str | None = None
    answer: str
    route: str | None = None
    status: str | None = None
    trace_id: str | None = None
    approval: dict[str, Any] = Field(default_factory=dict)
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    model_usage: dict[str, Any] = Field(default_factory=dict)
    flow: list[dict[str, Any]] = Field(default_factory=list)


class SessionMessagesResponse(BaseModel):
    session_id: str
    messages: list[dict[str, Any]] = Field(default_factory=list)
    assistant_message_ids: list[str] = Field(default_factory=list)


class FeedbackResponse(BaseModel):
    saved: bool
    feedback: dict[str, Any]


class WeComAIBotResponse(BaseModel):
    msgtype: str
    markdown: dict[str, str]


class ErrorResponse(BaseModel):
    detail: Any


COMMON_ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "请求参数非法或业务校验失败。"},
    401: {"model": ErrorResponse, "description": "Bearer 鉴权失败。"},
    403: {"model": ErrorResponse, "description": "渠道、发布可见度或运行主体未授权。"},
    404: {"model": ErrorResponse, "description": "Agent、release 或审批记录不存在。"},
    409: {"model": ErrorResponse, "description": "审批冲突或发布状态不可运行。"},
    422: {"model": ErrorResponse, "description": "请求体校验失败。"},
    500: {"model": ErrorResponse, "description": "服务内部错误。"},
}


def require_api_token(authorization: str | None = Header(default=None)) -> None:
    token = os.environ.get("API_BEARER_TOKEN")
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=401, detail="unauthorized")


def validate_reply_url(reply_url: str | None) -> str | None:
    if not reply_url:
        return None
    parsed = urlparse(reply_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=422, detail="reply_url 必须是 http 或 https URL")
    return reply_url


def reply_url_for_log(reply_url: str) -> str:
    parsed = urlparse(reply_url)
    if not parsed.scheme or not parsed.netloc:
        return "[invalid-url]"
    return parsed._replace(query="", fragment="").geturl()


def build_gateway_run_response(
    *,
    run_id: str,
    session_id: str,
    payload: GatewayRunRequest,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "session_id": session_id,
        "agent_id": payload.agent_id,
        "release_id": payload.release_id,
        "actor_id": payload.actor_id,
        "channel": payload.channel,
        "answer": result.get("answer") or "未生成回答",
        "route": result.get("route"),
        "status": result.get("status"),
        "trace_id": result.get("trace_id"),
        "tool_observations": result.get("tool_observations") or [],
        "approval_requests": result.get("approval_requests") or [],
        "model_usage": result.get("model_usage") or {},
        "flow": result.get("observability") or [],
    }


def build_gateway_reply_body(payload: GatewayRunRequest, response: dict[str, Any]) -> dict[str, Any]:
    answer = response.get("answer") or "未生成回答"
    body = {
        "cid": payload.cid or payload.metadata.get("cid"),
        "content": answer,
        "answer": answer,
        "run_id": response.get("run_id"),
        "session_id": response.get("session_id"),
        "trace_id": response.get("trace_id"),
        "status": response.get("status"),
        "route": response.get("route"),
    }
    for key in ("event_id", "mid", "sender_uid"):
        value = getattr(payload, key) or payload.metadata.get(key)
        if value:
            body[key] = value
    return {key: value for key, value in body.items() if value is not None}


def build_webhook_reply_body(payload: WebhookRunRequest, response: dict[str, Any]) -> dict[str, Any]:
    answer = response.get("answer") or "未生成回答"
    body = {
        "cid": payload.cid,
        "content": answer,
        "answer": answer,
        "run_id": response.get("run_id"),
        "session_id": response.get("session_id"),
        "trace_id": response.get("trace_id"),
        "status": response.get("status"),
        "route": response.get("route"),
        "event_id": payload.event_id or payload.metadata.get("event_id"),
        "mid": payload.mid or payload.metadata.get("mid"),
        "sender_uid": payload.sender_uid,
    }
    return {key: value for key, value in body.items() if value is not None}


def post_gateway_reply(reply_url: str, body: dict[str, Any], *, timeout: int = 10) -> None:
    request = urllib.request.Request(
        reply_url,
        data=json.dumps(body, ensure_ascii=False, default=str).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
        log_event(
            LOGGER,
            logging.INFO,
            "gateway_reply_url_posted",
            run_id=body.get("run_id"),
            trace_id=body.get("trace_id"),
            status_code=getattr(response, "status", None),
            reply_url=reply_url_for_log(reply_url),
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        log_event(
            LOGGER,
            logging.WARNING,
            "gateway_reply_url_error",
            run_id=body.get("run_id"),
            trace_id=body.get("trace_id"),
            status_code=exc.code,
            reply_url=reply_url_for_log(reply_url),
            error_type=type(exc).__name__,
            error=str(exc),
            response_body=detail,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log_event(
            LOGGER,
            logging.WARNING,
            "gateway_reply_url_error",
            run_id=body.get("run_id"),
            trace_id=body.get("trace_id"),
            reply_url=reply_url_for_log(reply_url),
            error_type=type(exc).__name__,
            error=str(exc),
        )


def run_configured_request(
    *,
    agent_id: str,
    release_id: str,
    actor_id: str,
    user_id: str,
    channel: str,
    session_id: str,
    message: str,
    source: str,
    metadata: dict[str, Any],
    reply_url: str | None,
    run_id: str,
) -> dict[str, Any]:
    context = runtime_context.resolve_runtime_context(
        agent_id,
        release_id,
        actor_id,
        channel,
    )
    result = run_configured_turn(
        context,
        message=message,
        session_id=session_id,
        user_id=user_id,
        history=[],
        settings=SETTINGS,
        metadata={**metadata, "gateway_run_id": run_id, "reply_url": reply_url},
        approval_store=default_approval_store,
    )
    answer = result.get("answer") or "未生成回答"
    try:
        save_chat_turn(
            SETTINGS,
            session_id=session_id,
            user_id=user_id,
            user_message=message,
            assistant_message=answer,
            result=result,
            source=source,
            request_metadata=metadata,
        )
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "gateway_chat_persist_error",
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    return result


def run_configured_webhook_request(
    *,
    agent_id: str,
    version: int,
    actor_id: str,
    user_id: str,
    channel: str,
    session_id: str,
    message: str,
    metadata: dict[str, Any],
    reply_url: str | None,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, object]]:
    context = runtime_context.resolve_runtime_context_by_version(
        agent_id,
        version,
        actor_id,
        channel,
    )
    release = context.get("release") if isinstance(context.get("release"), dict) else {}
    release_id = str(release.get("release_id") or "")
    result = run_configured_turn(
        context,
        message=message,
        session_id=session_id,
        user_id=user_id,
        history=[],
        settings=SETTINGS,
        metadata={**metadata, "gateway_run_id": run_id, "reply_url": reply_url},
        approval_store=default_approval_store,
    )
    answer = result.get("answer") or "未生成回答"
    try:
        save_chat_turn(
            SETTINGS,
            session_id=session_id,
            user_id=user_id,
            user_message=message,
            assistant_message=answer,
            result=result,
            source="webhook",
            request_metadata={**metadata, "release_id": release_id, "version": version},
        )
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "gateway_chat_persist_error",
            run_id=run_id,
            session_id=session_id,
            user_id=user_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
    return result, release


@app.get("/", tags=["system"], summary="Root", response_model=RootResponse)
def root() -> RootResponse:
    return {"ok": True, "service": "agentic-rag-api"}


@app.get("/healthz", tags=["system"], summary="Health Check", response_model=HealthzResponse)
def healthz() -> HealthzResponse:
    return {
        "ok": True,
        "instance_id": SETTINGS.observability.instance_id,
        "loop_mode": SETTINGS.agent.loop_mode,
        "embedding_provider": SETTINGS.embedding.provider,
    }


@app.get(
    "/metrics",
    tags=["system"],
    summary="Prometheus Metrics",
    description="Prometheus scrape endpoint. 本地测试默认不要求 Bearer 鉴权，生产部署应通过网络策略限制访问。",
    include_in_schema=True,
)
def metrics() -> Response:
    return metrics_response()


@app.post(
    "/v1/chat",
    dependencies=[Depends(require_api_token)],
    tags=["chat"],
    summary="Run Legacy Chat Turn",
    description="旧客服单轮问答入口，按 `session_id` 恢复最近会话历史并返回本轮回答。",
    response_model=ChatTurnResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def chat(payload: ChatRequest) -> ChatTurnResponse:
    session_id = normalize_session_id(payload.session_id)
    try:
        response = handle_chat_turn(
            ChatTurnInput(
                message=payload.message,
                session_id=session_id,
                product_id=payload.product_id or SETTINGS.app.product_id,
                domain_hint=payload.domain_hint,
                source="api",
                metadata=payload.metadata,
            ),
            DEPENDENCIES,
            SETTINGS,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "api_chat_error",
            session_id=session_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="agent turn failed") from exc
    return response


@app.post(
    "/v1/gateway/runs",
    dependencies=[Depends(require_api_token)],
    tags=["gateway"],
    summary="Run Configured Release by Release ID",
    description=(
        "平台调试入口。调用方显式传入 `agent_id + release_id + actor_id + channel`，"
        "Resolver 解析发布快照并注入 system prompt、model、active tools。"
    ),
    response_model=GatewayRunResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def gateway_run(payload: GatewayRunRequest) -> GatewayRunResponse:
    if payload.stream:
        raise HTTPException(status_code=422, detail="首期 gateway 暂不支持 stream=true")
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    reply_url = validate_reply_url(payload.reply_url)

    run_id = str(uuid4())
    session_id = normalize_session_id(payload.session_id or f"gateway-{run_id}")
    try:
        user_id = str(payload.sender_uid or payload.actor_id).strip() or derive_user_id_from_session(session_id)
        result = run_configured_request(
            agent_id=payload.agent_id,
            release_id=payload.release_id,
            actor_id=payload.actor_id,
            user_id=user_id,
            channel=payload.channel,
            message=message,
            session_id=session_id,
            source="gateway",
            metadata={
                **payload.metadata,
                "channel": payload.channel,
                "actor_id": payload.actor_id,
                "sender_uid": payload.sender_uid,
                "gateway_run_id": run_id,
            },
            reply_url=reply_url,
            run_id=run_id,
        )
    except runtime_context.RuntimeContextError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "gateway_run_error",
            run_id=run_id,
            agent_id=payload.agent_id,
            release_id=payload.release_id,
            actor_id=payload.actor_id,
            channel=payload.channel,
            session_id=session_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="gateway run failed") from exc

    response = build_gateway_run_response(run_id=run_id, session_id=session_id, payload=payload, result=result)
    if reply_url:
        post_gateway_reply(reply_url, build_gateway_reply_body(payload, response))
    return response


@app.post(
    "/v1/webhooks/runs",
    dependencies=[Depends(require_api_token)],
    tags=["webhooks"],
    summary="Run Webhook Invocation by Version",
    description=(
        "Webhook/OA/IM 专用入口。调用方传入 `agent_id + version + channel + cid + sender_uid`，"
        "Runner 先解析版本号对应的 release，再以 `cid` 作为 `session_id`、"
        "`sender_uid` 作为当前发言人身份执行。"
    ),
    response_model=WebhookRunResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def webhook_run(payload: WebhookRunRequest) -> WebhookRunResponse:
    if payload.stream:
        raise HTTPException(status_code=422, detail="首期 webhook 暂不支持 stream=true")
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message 不能为空")
    reply_url = validate_reply_url(payload.reply_url)

    run_id = str(uuid4())
    session_id = normalize_session_id(payload.cid)
    actor_id = payload.sender_uid.strip()
    user_id = actor_id or derive_user_id_from_session(session_id)
    try:
        result, release = run_configured_webhook_request(
            agent_id=payload.agent_id,
            version=payload.version,
            actor_id=actor_id,
            user_id=user_id,
            channel=payload.channel,
            session_id=session_id,
            message=message,
            metadata={
                **payload.metadata,
                "channel": payload.channel,
                "sender_uid": payload.sender_uid,
                "cid": payload.cid,
                "event_id": payload.event_id,
                "mid": payload.mid,
                "webhook_run_id": run_id,
            },
            reply_url=reply_url,
            run_id=run_id,
        )
    except runtime_context.RuntimeContextError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "webhook_run_error",
            run_id=run_id,
            agent_id=payload.agent_id,
            version=payload.version,
            actor_id=actor_id,
            channel=payload.channel,
            session_id=session_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="webhook run failed") from exc

    response = {
        "run_id": run_id,
        "session_id": session_id,
        "agent_id": payload.agent_id,
        "release_id": release.get("release_id"),
        "version": release.get("version"),
        "actor_id": actor_id,
        "user_id": user_id,
        "channel": payload.channel,
        "answer": result.get("answer") or "未生成回答",
        "route": result.get("route"),
        "status": result.get("status"),
        "trace_id": result.get("trace_id"),
        "tool_observations": result.get("tool_observations") or [],
        "approval_requests": result.get("approval_requests") or [],
        "model_usage": result.get("model_usage") or {},
        "flow": result.get("observability") or [],
    }
    if reply_url:
        post_gateway_reply(reply_url, build_webhook_reply_body(payload, response))
    return response


@app.post(
    "/v1/gateway/approvals/{approval_id}/decide",
    dependencies=[Depends(require_api_token)],
    tags=["approvals"],
    summary="Decide Pending Approval",
    description="提交审批结果，并在批准时恢复原始 tool call 所在的运行现场。",
    response_model=ApprovalDecisionResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def gateway_decide_approval(approval_id: str, payload: ApprovalDecisionRequest) -> ApprovalDecisionResponse:
    try:
        decided_by = payload.reviewer_id()
        result = decide_approval(
            approval_id,
            ApprovalDecision(
                decision=payload.decision,
                decided_by=decided_by,
                comment=payload.comment,
            ),
            approval_store=default_approval_store,
            settings=SETTINGS,
        )
    except runtime_context.RuntimeContextError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "gateway_approval_error",
            approval_id=approval_id,
            decision=payload.decision,
            decided_by=payload.decided_by or payload.actor_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="approval decision failed") from exc
    return {
        "run_id": result.get("run_id"),
        "session_id": result.get("session_id"),
        "agent_id": result.get("agent_id"),
        "release_id": result.get("release_id"),
        "actor_id": result.get("actor_id"),
        "channel": result.get("channel"),
        "answer": result.get("answer") or "未生成回答",
        "route": result.get("route"),
        "status": result.get("status"),
        "trace_id": result.get("trace_id"),
        "approval": result.get("approval"),
        "tool_observations": result.get("tool_observations") or [],
        "model_usage": result.get("model_usage") or {},
        "flow": result.get("observability") or result.get("flow") or [],
    }


@app.post(
    "/v1/chat/stream",
    dependencies=[Depends(require_api_token)],
    tags=["chat"],
    summary="Run Legacy Chat Turn as SSE",
    description="旧客服 SSE 流式事件流。该接口返回 `text/event-stream`，docs 中仅展示输入模型。",
    responses=COMMON_ERROR_RESPONSES,
)
def chat_stream(payload: ChatRequest) -> StreamingResponse:
    session_id = normalize_session_id(payload.session_id)
    events: queue.Queue[dict[str, Any] | None] = queue.Queue()

    def stream_handler(event: dict[str, Any]) -> None:
        events.put(event)

    def worker() -> None:
        try:
            handle_chat_turn(
                ChatTurnInput(
                    message=payload.message,
                    session_id=session_id,
                    product_id=payload.product_id or SETTINGS.app.product_id,
                    domain_hint=payload.domain_hint,
                    source="api_stream",
                    metadata=payload.metadata,
                    stream_handler=stream_handler,
                ),
                DEPENDENCIES,
                SETTINGS,
            )
        except Exception as exc:
            events.put(
                {
                    "type": "error",
                    "message": "agent turn failed",
                    "payload": {"event": "error", "error_type": type(exc).__name__, "error": str(exc)},
                    "session_id": session_id,
                }
            )
        finally:
            events.put(None)

    def event_generator():
        thread = threading.Thread(target=worker, name=f"chat-stream-{session_id}", daemon=True)
        thread.start()
        while True:
            event = events.get()
            if event is None:
                yield "event: done\ndata: {}\n\n"
                break
            yield "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post(
    "/v1/wecom/aibot",
    tags=["wecom"],
    summary="Run WeCom HTTP Bot Turn",
    description="企业微信 HTTP 入口。会把企业微信消息归一化后送入旧客服对话链路。",
    response_model=WeComAIBotResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def wecom_aibot(payload: WeComAIBotRequest) -> WeComAIBotResponse:
    try:
        turn = build_wecom_turn(payload.model_dump(by_alias=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    session_id = normalize_session_id(turn.session_id)
    try:
        response = handle_chat_turn(
            ChatTurnInput(
                message=turn.message,
                session_id=session_id,
                product_id=SETTINGS.app.product_id,
                source="wecom",
                metadata=turn.metadata,
            ),
            DEPENDENCIES,
            SETTINGS,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "api_wecom_error",
            session_id=session_id,
            msgid=payload.msgid,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="agent turn failed") from exc

    return {
        "msgtype": "markdown",
        "markdown": {"content": response.get("answer") or "未生成回答"},
    }


@app.get(
    "/v1/sessions/{session_id}/messages",
    dependencies=[Depends(require_api_token)],
    tags=["sessions"],
    summary="Get Recent Session Messages",
    description="读取最近会话消息和 assistant message ids，供调试或前端恢复上下文使用。",
    response_model=SessionMessagesResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def session_messages(session_id: str, limit: int = 20) -> SessionMessagesResponse:
    session_id = normalize_session_id(session_id)
    safe_limit = max(1, min(int(limit), 100))
    try:
        history, assistant_message_ids = load_recent_conversation(SETTINGS, session_id, limit=safe_limit)
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "api_session_messages_error",
            session_id=session_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="load session messages failed") from exc
    return {
        "session_id": session_id,
        "messages": history,
        "assistant_message_ids": assistant_message_ids,
    }


@app.post(
    "/v1/messages/{assistant_message_id}/feedback",
    dependencies=[Depends(require_api_token)],
    tags=["sessions"],
    summary="Save Assistant Message Feedback",
    description="保存某条 assistant 消息的正负反馈，用于离线评估和质量回看。",
    response_model=FeedbackResponse,
    responses=COMMON_ERROR_RESPONSES,
)
def message_feedback(assistant_message_id: str, payload: FeedbackRequest) -> FeedbackResponse:
    session_id = normalize_session_id(payload.session_id)
    user_id = derive_user_id_from_session(session_id)
    try:
        row = save_message_feedback(
            SETTINGS,
            assistant_message_id=assistant_message_id,
            session_id=session_id,
            user_id=user_id,
            rating=payload.rating,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log_event(
            LOGGER,
            logging.ERROR,
            "api_feedback_error",
            session_id=session_id,
            assistant_message_id=assistant_message_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        raise HTTPException(status_code=500, detail="save feedback failed") from exc
    return {"saved": True, "feedback": row}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "apps.api_app:app",
        host=os.environ.get("API_HOST", "127.0.0.1"),
        port=int(os.environ.get("API_PORT", "8000")),
        workers=int(os.environ.get("API_WORKERS", "1")),
    )
