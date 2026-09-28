from __future__ import annotations

from contextlib import contextmanager

from aegora_runtime.local_aicoin_tools import save_user_memory
from aegora_runtime.memory.models import MemoryItem, MemoryScope, MemoryWrite
from aegora_runtime.memory.native_pg import NativePgMemoryProvider
from aegora_runtime.memory.policy import (
    AGENT_PRIVATE,
    TENANT_REQUIRED,
    USER_CONTROLLED,
    MemoryGovernance,
    MemoryNamespacePolicy,
    can_recall,
    write_scope_for,
)
from aegora_runtime.memory.service import MemoryService


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
            scope=scope.memory_scope,
            namespace=scope.namespace,
            source_agent_id=memory.source_agent_id or scope.agent_id,
            metadata={
                "user_id": scope.user_id,
                "session_id": scope.session_id,
                "agent_id": scope.agent_id if scope.memory_scope == "user_agent" else None,
            },
        )
        self.items.setdefault(scope.user_id, {})[memory.key] = item
        return item

    def get_current(self, *, scope: MemoryScope, key: str) -> MemoryItem | None:
        return self.items.get(scope.user_id, {}).get(key)

    def forget(
        self,
        *,
        scope: MemoryScope,
        key: str,
        reason=None,
        source_trace_id=None,
        source_kind="explicit",
    ) -> bool:
        del reason, source_trace_id, source_kind
        return self.items.get(scope.user_id, {}).pop(key, None) is not None

    def expire_due(self, *, tenant_id=None, limit=500) -> int:
        del tenant_id, limit
        return 0


class ExplodingMemoryProvider:
    def recall(self, *, scope: MemoryScope, query: str, limit: int):
        raise RuntimeError("recall unavailable")

    def get_current(self, *, scope: MemoryScope, key: str):
        raise RuntimeError("lookup unavailable")

    def remember(self, *, scope: MemoryScope, memory: MemoryWrite):
        raise RuntimeError("write unavailable")

    def forget(self, *, scope: MemoryScope, key: str, **kwargs):
        raise RuntimeError("forget unavailable")

    def expire_due(self, *, tenant_id=None, limit=500):
        raise RuntimeError("expiry unavailable")


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


def test_memory_service_recall_can_be_disabled_without_disabling_writes() -> None:
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


def test_user_global_memory_is_source_agent_private_until_user_shares() -> None:
    item = MemoryItem(
        memory_id="1",
        memory_type="semantic",
        key="language",
        content="Python",
        scope="user_global",
        namespace="preferences",
        source_agent_id="agent-a",
    )
    own = MemoryGovernance(agent_id="agent-a", user_preferences={"preferences": False})
    other_denied = MemoryGovernance(agent_id="agent-b", user_preferences={"preferences": False})
    other_allowed = MemoryGovernance(agent_id="agent-b", user_preferences={"preferences": True})

    assert can_recall(item, own) is True
    assert can_recall(item, other_denied) is False
    assert can_recall(item, other_allowed) is True


def test_public_agent_requires_tenant_policy_before_reading_shared_memory() -> None:
    item = MemoryItem(
        memory_id="1",
        memory_type="semantic",
        key="language",
        content="Python",
        scope="user_global",
        namespace="preferences",
        source_agent_id="agent-a",
    )
    blocked = MemoryGovernance(
        agent_id="public-agent",
        agent_visibility="public",
        user_preferences={"preferences": True},
        namespace_policies={
            "preferences": MemoryNamespacePolicy(
                namespace="preferences",
                mode=USER_CONTROLLED,
                allow_public_agents=False,
            )
        },
    )
    allowed = MemoryGovernance(
        agent_id="public-agent",
        agent_visibility="public",
        user_preferences={"preferences": True},
        namespace_policies={
            "preferences": MemoryNamespacePolicy(
                namespace="preferences",
                mode=USER_CONTROLLED,
                allow_public_agents=True,
            )
        },
    )

    assert can_recall(item, blocked) is False
    assert can_recall(item, allowed) is True


def test_tenant_required_memory_ignores_user_share_toggle_but_respects_public_gate() -> None:
    item = MemoryItem(
        memory_id="org-1",
        memory_type="profile",
        key="department",
        content="AI Platform",
        scope="tenant_user",
        namespace="org_profile",
    )
    private_agent = MemoryGovernance(
        agent_id="agent-private",
        user_preferences={"org_profile": False},
        namespace_policies={
            "org_profile": MemoryNamespacePolicy(
                namespace="org_profile",
                mode=TENANT_REQUIRED,
                allow_public_agents=False,
            )
        },
    )
    public_blocked = MemoryGovernance(
        agent_id="agent-public",
        agent_visibility="public",
        user_preferences={"org_profile": False},
        namespace_policies=private_agent.namespace_policies,
    )
    public_allowed = MemoryGovernance(
        agent_id="agent-public",
        agent_visibility="public",
        user_preferences={"org_profile": False},
        namespace_policies={
            "org_profile": MemoryNamespacePolicy(
                namespace="org_profile",
                mode=TENANT_REQUIRED,
                allow_public_agents=True,
            )
        },
    )

    assert can_recall(item, private_agent) is True
    assert can_recall(item, public_blocked) is False
    assert can_recall(item, public_allowed) is True


def test_agent_overlay_never_crosses_agent_boundary() -> None:
    item = MemoryItem(
        memory_id="overlay-1",
        memory_type="semantic",
        key="testing",
        content="pytest",
        scope="user_agent",
        namespace="coding",
        metadata={"agent_id": "coding-agent"},
    )

    assert can_recall(item, MemoryGovernance(agent_id="coding-agent")) is True
    assert can_recall(item, MemoryGovernance(agent_id="hr-agent")) is False


def test_write_scope_follows_namespace_policy() -> None:
    governance = MemoryGovernance(
        agent_id="agent-a",
        namespace_policies={
            "preferences": MemoryNamespacePolicy("preferences", USER_CONTROLLED),
            "private_notes": MemoryNamespacePolicy("private_notes", AGENT_PRIVATE),
            "org_profile": MemoryNamespacePolicy("org_profile", TENANT_REQUIRED),
        },
    )

    assert write_scope_for(governance, "preferences") == "user_global"
    assert write_scope_for(governance, "private_notes") == "user_agent"
    assert write_scope_for(governance, "org_profile") == "tenant_user"


class FakeCursor:
    def __init__(self, *, fetchone_results=None, fetchall_results=None) -> None:
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])
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
        return self.fetchone_results.pop(0) if self.fetchone_results else None

    def fetchall(self):
        return self.fetchall_results.pop(0) if self.fetchall_results else []


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


def normalized_row(**overrides):
    row = {
        "id": 11,
        "tenant_id": "default",
        "subject_user_id": "user-1",
        "scope": "user_global",
        "namespace": "preferences",
        "agent_id": None,
        "memory_type": "semantic",
        "memory_key": "preferred_language",
        "content": "Python",
        "source_agent_id": "agent-a",
        "source_session_id": "session-a",
        "source_trace_id": "trace-a",
        "confidence": 1.0,
        "importance": 0.5,
        "status": "active",
        "version": 1,
        "supersedes_id": None,
        "valid_from": None,
        "valid_until": None,
        "created_at": None,
        "updated_at": None,
    }
    row.update(overrides)
    return row


def test_native_pg_recall_maps_normalized_memory_items(monkeypatch) -> None:
    cursor = FakeCursor(fetchall_results=[[normalized_row()]])
    monkeypatch.setattr(
        "aegora_runtime.memory.native_pg.connect",
        fake_connect(FakeConnection(cursor)),
    )
    provider = NativePgMemoryProvider(settings=object())

    items = provider.recall(
        scope=MemoryScope(
            user_id="user-1",
            session_id="session-b",
            agent_id="agent-b",
            tenant_id="default",
        ),
        query="what do I prefer?",
        limit=8,
    )

    assert cursor.calls[0][1] == ("default", "user-1", "agent-b", 8)
    assert "(valid_until IS NULL OR valid_until > now())" in cursor.calls[0][0]
    assert [(item.memory_type, item.key, item.version) for item in items] == [
        ("semantic", "preferred_language", 1)
    ]
    assert items[0].source_agent_id == "agent-a"
    assert items[0].metadata["source_trace_id"] == "trace-a"


def test_save_user_memory_routes_through_governed_memory_service(monkeypatch) -> None:
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
            {
                "session_id": "session-9",
                "user_id": "employee-42",
                "trace_id": "trace-9",
            },
        )(),
        "context": {
            "runtime_context": {
                "agent": {"id": "agent-support"},
                "release": {"visibility": "private"},
                "memory_governance": {
                    "tenant_id": "tenant-a",
                    "namespace_policies": {
                        "preferences": {
                            "mode": "user_controlled",
                            "allow_public_agents": False,
                        }
                    },
                    "user_preferences": {"preferences": False},
                },
            }
        },
    }

    result = save_user_memory(object(), state, "response_style", "concise")

    assert captured["scope"].user_id == "employee-42"
    assert captured["scope"].tenant_id == "tenant-a"
    assert captured["scope"].memory_scope == "user_global"
    assert captured["scope"].agent_id == "agent-support"
    assert captured["memory"].source_agent_id == "agent-support"
    assert captured["memory"].source_trace_id == "trace-9"
    assert result["scope"] == "user_global"
    assert result["namespace"] == "preferences"


def test_save_user_memory_rejects_tenant_managed_namespace(monkeypatch) -> None:
    state = {
        "request": type("Req", (), {"session_id": "s1", "user_id": "u1", "trace_id": "t1"})(),
        "context": {
            "runtime_context": {
                "agent": {"id": "agent-a"},
                "memory_governance": {
                    "namespace_policies": {
                        "preferences": {
                            "mode": "tenant_required",
                            "allow_public_agents": False,
                        }
                    }
                },
            }
        },
    }

    result = save_user_memory(object(), state, "department", "AI")

    assert result["saved"] is False
    assert result["reason"] == "tenant_managed_namespace"


def test_native_pg_remember_versions_and_preserves_provenance(monkeypatch) -> None:
    current = normalized_row(id=7, content="Java", version=1)
    inserted = normalized_row(
        id=8,
        content="Python",
        version=2,
        supersedes_id=7,
        source_agent_id="agent-a",
        source_session_id="session-9",
        source_trace_id="trace-9",
    )
    cursor = FakeCursor(fetchone_results=[current, inserted])
    connection = FakeConnection(cursor)
    monkeypatch.setattr("aegora_runtime.memory.native_pg.connect", fake_connect(connection))
    provider = NativePgMemoryProvider(settings=object())

    item = provider.remember(
        scope=MemoryScope(
            user_id="employee-42",
            session_id="session-9",
            agent_id="agent-a",
            tenant_id="default",
            namespace="preferences",
            memory_scope="user_global",
        ),
        memory=MemoryWrite(
            key="preferred_language",
            value="Python",
            source_agent_id="agent-a",
            source_session_id="session-9",
            source_trace_id="trace-9",
        ),
    )

    update_calls = [call for call in cursor.calls if call[0].lstrip().startswith("UPDATE memory_items")]
    insert_calls = [call for call in cursor.calls if "INSERT INTO memory_items" in call[0]]
    assert update_calls[0][1] == (7,)
    assert insert_calls[0][1][-3:-1] == (2, 7)
    assert item.version == 2
    assert item.metadata["supersedes_id"] == 7
    assert item.metadata["source_trace_id"] == "trace-9"
    assert connection.commits == 1


def test_native_pg_expire_due_marks_items_and_records_event(monkeypatch) -> None:
    expired = normalized_row(
        id=12,
        source_kind="automatic",
        source_agent_id="agent-a",
        source_session_id="session-1",
        source_trace_id="trace-1",
    )
    cursor = FakeCursor(fetchall_results=[[expired]])
    connection = FakeConnection(cursor)
    monkeypatch.setattr("aegora_runtime.memory.native_pg.connect", fake_connect(connection))
    provider = NativePgMemoryProvider(settings=object())

    count = provider.expire_due(tenant_id="default", limit=10)

    assert count == 1
    assert cursor.calls[0][1] == ("default", "default", 10)
    assert "SET status = 'expired'" in cursor.calls[0][0]
    event_calls = [call for call in cursor.calls if "INSERT INTO memory_events" in call[0]]
    assert len(event_calls) == 1
    assert event_calls[0][1][6] == "expired"
    assert event_calls[0][1][11] == "valid_until_elapsed"
    assert connection.commits == 1
