from __future__ import annotations

from contextlib import contextmanager

from aegora_runtime.config import load_settings
from aegora_runtime.service import ChatTurnInput, handle_chat_turn


@contextmanager
def fake_lock(_settings, session_id):
    yield


def test_handle_chat_turn_is_session_scoped(monkeypatch) -> None:
    captured = {}
    stream_events = []

    monkeypatch.setattr("aegora_runtime.service.session_execution_lock", fake_lock)
    monkeypatch.setattr(
        "aegora_runtime.service.load_recent_conversation",
        lambda _settings, session_id, limit=20: ([{"role": "user", "content": "上文"}], [7]),
    )

    def fake_run_agent(request, _deps, _settings):
        captured["request"] = request
        return {
            "answer": "回答",
            "route": "chat",
            "status": "completed",
            "trace_id": "turn-1",
            "retrieved_faqs": [],
            "observability": [],
        }

    def fake_save_chat_turn(_settings, **kwargs):
        captured["saved"] = kwargs
        return 42

    monkeypatch.setattr("aegora_runtime.service.run_agent", fake_run_agent)
    monkeypatch.setattr("aegora_runtime.service.save_chat_turn", fake_save_chat_turn)

    result = handle_chat_turn(
        ChatTurnInput(
            message="  你好  ",
            session_id=" external-session ",
            product_id="aicoin",
            metadata={"channel": "im"},
            stream_handler=stream_events.append,
        ),
        dependencies=None,
        settings=load_settings(env_path=None),
    )

    request = captured["request"]
    saved = captured["saved"]
    assert request.query == "你好"
    assert request.session_id == "external-session"
    assert request.user_id == "session-owner:external-session"
    assert request.history == [{"role": "user", "content": "上文"}]
    assert saved["session_id"] == "external-session"
    assert saved["user_id"] == "session-owner:external-session"
    assert saved["source"] == "api"
    assert saved["request_metadata"] == {"channel": "im"}
    assert result["assistant_message_id"] == 42
    assert result["session_id"] == "external-session"
    assert stream_events[-1]["type"] == "final_answer"
    assert stream_events[-1]["payload"]["assistant_message_id"] == 42


def test_handle_chat_turn_uses_session_owner_user_id_by_default(monkeypatch) -> None:
    captured = {}

    monkeypatch.setattr("aegora_runtime.service.session_execution_lock", fake_lock)
    monkeypatch.setattr(
        "aegora_runtime.service.load_recent_conversation",
        lambda _settings, session_id, limit=20: ([], []),
    )

    def fake_run_agent(request, _deps, _settings):
        captured["request"] = request
        return {
            "answer": "回答",
            "route": "chat",
            "status": "completed",
            "trace_id": "turn-2",
            "retrieved_faqs": [],
            "observability": [],
        }

    monkeypatch.setattr("aegora_runtime.service.run_agent", fake_run_agent)
    monkeypatch.setattr("aegora_runtime.service.save_chat_turn", lambda _settings, **kwargs: 7)

    result = handle_chat_turn(
        ChatTurnInput(
            message="你好",
            session_id="group-1",
            metadata={},
        ),
        dependencies=None,
        settings=load_settings(env_path=None),
    )

    assert result["user_id"] == "session-owner:group-1"
    assert captured["request"].user_id == "session-owner:group-1"


def test_handle_chat_turn_rejects_empty_message() -> None:
    try:
        handle_chat_turn(ChatTurnInput(message=" ", session_id="s1"), None, load_settings(env_path=None))
    except ValueError as exc:
        assert str(exc) == "message is required"
    else:
        raise AssertionError("expected ValueError")
