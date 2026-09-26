from __future__ import annotations

from contextlib import contextmanager

from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite
from aegora_runtime.memory.native_pg import NativePgMemoryProvider
from aegora_runtime.memory.service import MemoryService
from aegora_runtime.local_aicoin_tools import save_user_memory


class DictMemoryProvider:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, MemoryItem]] = {}

    def recall(self, *, scope: MemoryScope, query: str, limit: int) -> list[MemoryItem]:
        del query
        return list(self.items.get(scope.user_id, {}).values())[:limit]

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite) -> MemoryItem:
        item = MemoryItem(
            memory_id=f"{scope.user_id}:{memory.key}",
            memory_type=memory.memory_type,
            key=memory.key,
            content=memory.value,
            metadata={"user_id": scope.user_id, "session_id": scope.session_id},
        )
        self.items.setdefault(scope.user_id, {})[memory.key] = item
        return item

    def forget(self, *, scope: MemoryScope, key: str) -> bool:
        return self.items.get(scope.user_id, {}).pop(key, None) is not None


class ExplodingMemoryProvider:
    def recall(self, *, scope: MemoryScope, query: str, limit: int):
        raise RuntimeError("recall unavailable")

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite):
        raise RuntimeError("write unavailable")

    def forget(self, *, scope: MemoryScope, key: str):
        raise RuntimeError("forget unavailable")


def test_memory_service_recalls_same_user_across_sessions_and_isolates_users() -> None:
    provider = DictMemoryProvider()
    service = MemoryService(provider, recall_limit=8, max_item_chars=400)

    write = service.remember(
        scope=MemoryScope(user_id="user-1", session_id="session-a"),
        memory=MemoryWrite(key="preferred_language", value="Python"),
    )

    assert write.saved is True
    recalled = service.recall(
        scope=MemoryScope(user_id="user-1", session_id="session-b"),
        query="写一个排序算法",
    )
    assert [(item.key, item.content) for item in recalled] == [("preferred_language", "Python")]

    other_user = service.recall(
        scope=MemoryScope(user_id="user-2", session_id="session-b"),
        query="写一个排序算法",
    )
    assert other_user == []


def test_memory_service_recall_can_be_disabled_without_disabling_legacy_writes() -> None:
    provider = DictMemoryProvider()
    service = MemoryService(provider, recall_enabled=False, write_enabled=True)

    result = service.remember(
        scope=MemoryScope(user_id="u1"),
        memory=MemoryWrite(key="style", value="concise"),
    )
    assert result.saved is True
    assert service.recall(scope=MemoryScope(user_id="u1"), query="q") == []


def test_memory_service_provider_failure_fails_open() -> None:
    service = MemoryService(ExplodingMemoryProvider())

    assert service.recall(scope=MemoryScope(user_id="u1"), query="q") == []
    result = service.remember(
        scope=MemoryScope(user_id="u1"),
        memory=MemoryWrite(key="style", value="concise"),
    )
    assert result.saved is False
    assert result.reason == "provider_error"
    assert service.forget(scope=MemoryScope(user_id="u1"), key="style") is False


class FakeCursor:
    def __init__(self, row=None) -> None:
        self.row = row
        self.calls = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, query, params=None) -> None:
        self.calls.append((query, params))
        if query.lstrip().startswith("UPDATE"):
            self.rowcount = 1

    def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0

    def cursor(self):
        return self._cursor

    def commit(self) -> None:
        self.commits += 1


def fake_connect(connection: FakeConnection):
    @contextmanager
    def _connect(_settings):
        yield connection

    return _connect


def test_native_pg_recall_maps_existing_user_memory_row(monkeypatch) -> None:
    cursor = FakeCursor(
        {
            "profile": {"role": "developer"},
            "facts": {"preferred_language": "Python"},
            "timeline": [{"content": "created Project Atlas"}],
            "summary": "Prefers concise technical answers.",
        }
    )
    monkeypatch.setattr(
        "aegora_runtime.memory.native_pg.connect",
        fake_connect(FakeConnection(cursor)),
    )
    provider = NativePgMemoryProvider(settings=object())

    items = provider.recall(
        scope=MemoryScope(user_id="user-1", session_id="session-b"),
        query="what do I prefer?",
        limit=8,
    )

    assert cursor.calls[0][1] == ("user-1",)
    assert [(item.memory_type, item.key) for item in items] == [
        ("semantic", "preferred_language"),
        ("profile", "role"),
        ("summary", "summary"),
        ("episodic", "timeline:0"),
    ]
    assert items[0].content == "Python"


def test_save_user_memory_routes_through_memory_service_with_request_user(monkeypatch) -> None:
    captured = {}

    class FakeResult:
        def to_tool_result(self):
            return {"saved": True, "key": "response_style"}

    class FakeService:
        def remember(self, *, scope, memory):
            captured["scope"] = scope
            captured["memory"] = memory
            return FakeResult()

    monkeypatch.setattr(
        "aegora_runtime.local_aicoin_tools.build_memory_service",
        lambda settings: FakeService(),
    )
    state = {
        "request": type(
            "Req",
            (),
            {"session_id": "session-9", "user_id": "employee-42"},
        )(),
        "context": {"runtime_context": {"agent": {"id": "agent-support"}}},
    }

    result = save_user_memory(object(), state, "response_style", "concise")

    assert captured["scope"].user_id == "employee-42"
    assert captured["scope"].session_id == "session-9"
    assert captured["scope"].agent_id == "agent-support"
    assert captured["memory"].value == "concise"
    assert result["scope"] == "user"
    assert result["user_id"] == "employee-42"


def test_native_pg_remember_uses_stable_user_id_not_session_id(monkeypatch) -> None:
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr("aegora_runtime.memory.native_pg.connect", fake_connect(connection))
    provider = NativePgMemoryProvider(settings=object())

    item = provider.remember(
        scope=MemoryScope(user_id="employee-42", session_id="web-session-9"),
        memory=MemoryWrite(key="response_style", value="concise"),
    )

    assert cursor.calls[0][1] == (
        "employee-42",
        "response_style",
        "concise",
        "response_style",
        "concise",
    )
    assert item.metadata["user_id"] == "employee-42"
    assert item.metadata["session_id"] == "web-session-9"
    assert connection.commits == 1
