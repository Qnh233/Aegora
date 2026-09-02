from __future__ import annotations

import pytest

from aegora_runtime.wecom import (
    build_feedback_id,
    build_stream_feedback_id,
    build_wecom_feedback,
    build_wecom_turn,
    chunk_text,
    format_answer_for_wecom,
    parse_feedback_id,
    parse_stream_feedback_id,
    strip_wecom_mention,
)


def test_strip_wecom_mention() -> None:
    assert strip_wecom_mention("@RobotA hello robot") == "hello robot"
    assert strip_wecom_mention("  @机器人  我要购买会员") == "我要购买会员"
    assert strip_wecom_mention("我要购买会员") == "我要购买会员"


def test_build_wecom_turn_from_http_payload() -> None:
    turn = build_wecom_turn(
        {
            "msgid": "m1",
            "aibotid": "bot",
            "chatid": "group-chat",
            "chattype": "group",
            "from": {"userid": "USERID"},
            "response_url": "https://example.test/response",
            "msgtype": "text",
            "text": {"content": "@RobotA hello robot"},
            "quote": {"msgtype": "text", "text": {"content": "这是今日的测试情况"}},
        }
    )

    assert turn.session_id == "USERID"
    assert turn.message == "hello robot\n\n引用内容：这是今日的测试情况"
    assert turn.metadata["transport"] == "http"
    assert turn.metadata["chatid"] == "group-chat"
    assert turn.metadata["from_userid"] == "USERID"


def test_build_wecom_turn_from_websocket_frame() -> None:
    turn = build_wecom_turn(
        {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "req-1"},
            "body": {
                "msgid": "m2",
                "from": {"userid": "USERID"},
                "msgtype": "text",
                "text": {"content": "你好"},
            },
        }
    )

    assert turn.session_id == "USERID"
    assert turn.message == "你好"
    assert turn.metadata["transport"] == "websocket"
    assert turn.metadata["ws_req_id"] == "req-1"


def test_build_wecom_turn_rejects_non_text() -> None:
    with pytest.raises(ValueError, match="only text"):
        build_wecom_turn({"from": {"userid": "u1"}, "msgtype": "image"})


def test_feedback_id_roundtrip() -> None:
    assert build_feedback_id(123) == "assistant_message:123"
    assert parse_feedback_id("assistant_message:123") == "123"
    assert parse_feedback_id("assistant_message:turn-1:assistant") == "turn-1:assistant"
    assert parse_feedback_id("external:123") is None
    assert build_stream_feedback_id("stream-1") == "wecom_stream:stream-1"
    assert parse_stream_feedback_id("wecom_stream:stream-1") == "stream-1"


def test_build_wecom_feedback_positive_event() -> None:
    feedback = build_wecom_feedback(
        {
            "cmd": "aibot_event_callback",
            "headers": {"req_id": "req-feedback"},
            "body": {
                "msgid": "m3",
                "chatid": "group-chat",
                "from": {"userid": "USERID"},
                "event": {
                    "eventtype": "feedback_event",
                    "feedback_event": {
                        "id": "assistant_message:42",
                        "type": 1,
                        "content": "回答有帮助",
                    },
                },
            },
        }
    )

    assert feedback.assistant_message_id == "42"
    assert feedback.session_id == "USERID"
    assert feedback.rating == "positive"
    assert feedback.reason == "回答有帮助"
    assert feedback.metadata["transport"] == "websocket"


def test_build_wecom_feedback_stream_event() -> None:
    feedback = build_wecom_feedback(
        {
            "from": {"userid": "USERID"},
            "event": {
                "eventtype": "feedback_event",
                "feedback_event": {
                    "id": "wecom_stream:stream-abc",
                    "type": 1,
                },
            },
        }
    )

    assert feedback.assistant_message_id is None
    assert feedback.stream_id == "stream-abc"
    assert feedback.rating == "positive"


def test_build_wecom_feedback_negative_event() -> None:
    feedback = build_wecom_feedback(
        {
            "from": {"userid": "USERID"},
            "event": {
                "eventtype": "feedback_event",
                "feedback_event": {
                    "id": "assistant_message:43",
                    "type": 2,
                    "content": "没有回答到点上",
                    "inaccurate_reason_list": ["答非所问"],
                },
            },
        }
    )

    assert feedback.assistant_message_id == "43"
    assert feedback.rating == "negative"
    assert "没有回答到点上" in feedback.reason
    assert "答非所问" in feedback.reason


def test_format_answer_for_wecom_uses_links_for_assets() -> None:
    text = format_answer_for_wecom(
        "请按步骤操作。",
        [{"platform": "APP", "url": "https://example.test/app.png"}],
    )

    assert "请按步骤操作。" in text
    assert "操作配图" in text
    assert "https://example.test/app.png" in text
    assert "![" not in text


def test_chunk_text_splits_long_content() -> None:
    chunks = chunk_text("a" * 12, max_chars=5)

    assert chunks == ["a" * 5, "a" * 5, "aa"]
