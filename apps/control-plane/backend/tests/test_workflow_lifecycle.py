from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app import api, db, workflow_capabilities
from app.models import (
    MCPConnectionDefinition,
    WorkflowCapabilityRequest,
    WorkflowVersionSummary,
)


def _connection() -> MCPConnectionDefinition:
    return MCPConnectionDefinition(
        connection_id="workflow.ops",
        name="Workflow MCP",
        status="active",
        transport="streamable_http",
        config={"url": "https://workflow.example.test/mcp"},
        config_version=1,
        config_hash="sha256:test",
    )


def _request(version: str = "v1") -> WorkflowCapabilityRequest:
    return WorkflowCapabilityRequest(
        name="Submit Expense",
        description="提交报销审批工作流",
        mcp_connection_id="workflow.ops",
        runner_name="submit_expense",
        version=version,
        requires_approval=True,
        scope_schema={"department": ["finance"]},
    )


def _summary(version: str = "v1", status: str = "active") -> WorkflowVersionSummary:
    now = datetime.now(timezone.utc)
    return WorkflowVersionSummary(
        workflow_id="expense.submit",
        version=version,
        lifecycle_status=status,
        manifest_hash="sha256:immutable",
        created_by="u_admin",
        created_at=now,
        activated_by="u_admin",
        activated_at=now,
        retired_by="u_admin" if status == "retired" else None,
        retired_at=now if status == "retired" else None,
    )


def _admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api, "require_current_platform_admin", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "fetch_mcp_connection", lambda _: _connection())


def test_publish_workflow_uses_immutable_version_store(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin(monkeypatch)
    captured = {}

    def publish(tool, actor_id):
        captured["tool"] = tool
        captured["actor_id"] = actor_id
        return _summary(tool.version)

    monkeypatch.setattr(api.db, "publish_workflow_version", publish)

    result = api.set_workflow_capability(
        "expense.submit",
        _request("v2"),
        authorization="Bearer admin",
    )

    assert captured["actor_id"] == "u_admin"
    assert captured["tool"].tool_id == "expense.submit"
    assert captured["tool"].version == "v2"
    assert result.manifest_hash == "sha256:immutable"


def test_publish_workflow_rejects_mutating_existing_version(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin(monkeypatch)
    monkeypatch.setattr(
        api.db,
        "publish_workflow_version",
        lambda *_: (_ for _ in ()).throw(ValueError("版本已发布且内容不可变")),
    )

    with pytest.raises(HTTPException) as exc_info:
        api.set_workflow_capability(
            "expense.submit",
            _request(),
            authorization="Bearer admin",
        )

    assert exc_info.value.status_code == 409
    assert "不可变" in str(exc_info.value.detail)


def test_list_and_retire_workflow_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin(monkeypatch)
    monkeypatch.setattr(api.db, "fetch_workflow_versions", lambda _: [_summary("v2"), _summary("v1", "retired")])
    monkeypatch.setattr(api.db, "retire_workflow_version", lambda *_: _summary("v2", "retired"))

    versions = api.list_workflow_versions("expense.submit", authorization="Bearer admin")
    retired = api.retire_workflow_version(
        "expense.submit",
        "v2",
        authorization="Bearer admin",
    )

    assert [item.version for item in versions] == ["v2", "v1"]
    assert retired.lifecycle_status == "retired"


def test_retire_missing_workflow_version_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    _admin(monkeypatch)
    monkeypatch.setattr(api.db, "retire_workflow_version", lambda *_: None)

    with pytest.raises(HTTPException) as exc_info:
        api.retire_workflow_version(
            "expense.submit",
            "v404",
            authorization="Bearer admin",
        )

    assert exc_info.value.status_code == 404


class _FakeCursor:
    def __init__(self, fetchone_values: list[object]) -> None:
        self._fetchone_values = list(fetchone_values)
        self.statements: list[tuple[str, object]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def execute(self, query: str, params=None) -> None:
        self.statements.append((" ".join(query.split()), params))

    def fetchone(self):
        if not self._fetchone_values:
            raise AssertionError("unexpected fetchone call")
        return self._fetchone_values.pop(0)


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def cursor(self) -> _FakeCursor:
        return self._cursor


def _workflow_tool(version: str = "v1"):
    return workflow_capabilities.build_workflow_capability(
        "expense.submit",
        _request(version),
        _connection(),
    )


def test_db_publish_workflow_creates_version_fact_and_runtime_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _workflow_tool("v1")
    manifest_hash = db.compute_tool_manifest_hash(tool)
    now = datetime.now(timezone.utc)
    cursor = _FakeCursor(
        [
            None,
            (
                tool.tool_id,
                tool.version,
                "active",
                manifest_hash,
                "u_admin",
                now,
                "u_admin",
                now,
                None,
                None,
            ),
        ]
    )
    monkeypatch.setattr(db.psycopg, "connect", lambda _url: _FakeConnection(cursor))

    summary = db.publish_workflow_version(tool, "u_admin")
    statements = [statement for statement, _ in cursor.statements]

    assert summary.lifecycle_status == "active"
    assert summary.manifest_hash == manifest_hash
    assert any("pg_advisory_xact_lock" in statement for statement in statements)
    assert any("INSERT INTO workflow_versions" in statement for statement in statements)
    assert any("INSERT INTO tools" in statement for statement in statements)


def test_db_publish_workflow_rejects_same_version_with_changed_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = _workflow_tool("v1")
    cursor = _FakeCursor([("sha256:different",)])
    monkeypatch.setattr(db.psycopg, "connect", lambda _url: _FakeConnection(cursor))

    with pytest.raises(ValueError, match="内容不可变"):
        db.publish_workflow_version(tool, "u_admin")

    statements = [statement for statement, _ in cursor.statements]
    assert not any("INSERT INTO workflow_versions" in statement for statement in statements)
    assert not any("INSERT INTO tools" in statement for statement in statements)


def test_db_retire_active_workflow_disables_runtime_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    cursor = _FakeCursor(
        [
            ("active",),
            (
                "expense.submit",
                "v1",
                "retired",
                "sha256:immutable",
                "u_admin",
                now,
                "u_admin",
                now,
                "u_admin",
                now,
            ),
        ]
    )
    monkeypatch.setattr(db.psycopg, "connect", lambda _url: _FakeConnection(cursor))

    retired = db.retire_workflow_version("expense.submit", "v1", "u_admin")
    statements = [statement for statement, _ in cursor.statements]

    assert retired is not None
    assert retired.lifecycle_status == "retired"
    assert any("UPDATE tools SET status = 'disabled'" in statement for statement in statements)
