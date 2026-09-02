from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from aegora_runtime import runtime_context


APPROVAL_PENDING = "pending"
APPROVAL_APPROVED = "approved"
APPROVAL_REJECTED = "rejected"
APPROVAL_EXECUTED = "executed"


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApprovalDecision:
    decision: str
    decided_by: str
    comment: str | None = None


class ApprovalStore(Protocol):
    def create_pending(
        self,
        *,
        snapshot: dict[str, Any],
        approval: dict[str, Any],
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]: ...

    def get(self, approval_id: str) -> dict[str, Any] | None: ...

    def decide(self, approval_id: str, decision: ApprovalDecision) -> dict[str, Any]: ...

    def mark_executed(self, approval_id: str, result: dict[str, Any]) -> None: ...


class InMemoryApprovalStore:
    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def create_pending(
        self,
        *,
        snapshot: dict[str, Any],
        approval: dict[str, Any],
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        now = utc_now()
        approval_id = approval.get("approval_id") or f"appr_{uuid4().hex}"
        record = {
            **approval,
            "approval_id": approval_id,
            "status": APPROVAL_PENDING,
            "snapshot": snapshot,
            "requested_at": isoformat(now),
            "expires_at": isoformat(now + timedelta(seconds=ttl_seconds)),
            "decided_at": None,
            "decided_by": None,
            "decision_comment": None,
        }
        self.records[str(approval_id)] = record
        return approval_response(record)

    def get(self, approval_id: str) -> dict[str, Any] | None:
        record = self.records.get(approval_id)
        return dict(record) if record else None

    def decide(self, approval_id: str, decision: ApprovalDecision) -> dict[str, Any]:
        record = self.records.get(approval_id)
        if not record:
            raise ApprovalError("审批请求不存在")
        if record["status"] != APPROVAL_PENDING:
            raise ApprovalError("审批请求已处理")
        if is_expired(record):
            record["status"] = "expired"
            raise ApprovalError("审批请求已过期")
        record["status"] = decision.decision
        record["decided_at"] = isoformat(utc_now())
        record["decided_by"] = decision.decided_by
        record["decision_comment"] = decision.comment
        return dict(record)

    def mark_executed(self, approval_id: str, result: dict[str, Any]) -> None:
        record = self.records.get(approval_id)
        if record:
            record["status"] = APPROVAL_EXECUTED
            record["result"] = json_safe(result)


class PostgresApprovalStore:
    def __init__(self) -> None:
        self._schema_ready = False

    def create_pending(
        self,
        *,
        snapshot: dict[str, Any],
        approval: dict[str, Any],
        ttl_seconds: int = 1800,
    ) -> dict[str, Any]:
        self.ensure_schema()
        now = utc_now()
        approval_id = str(approval.get("approval_id") or f"appr_{uuid4().hex}")
        expires_at = now + timedelta(seconds=ttl_seconds)
        from psycopg.types.json import Jsonb

        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO runner_approval_requests (
                        approval_id, run_id, session_id, trace_id, agent_id, release_id,
                        actor_id, channel, tool_id, runner_tool_id, runner_name,
                        tool_args_json, tool_policy_json, snapshot_json, status,
                        requested_at, expires_at
                    )
                    VALUES (
                        %(approval_id)s, %(run_id)s, %(session_id)s, %(trace_id)s,
                        %(agent_id)s, %(release_id)s, %(actor_id)s, %(channel)s,
                        %(tool_id)s, %(runner_tool_id)s, %(runner_name)s,
                        %(tool_args_json)s, %(tool_policy_json)s, %(snapshot_json)s,
                        'pending', %(requested_at)s, %(expires_at)s
                    )
                    """,
                    {
                        **approval,
                        "approval_id": approval_id,
                        "tool_args_json": Jsonb(approval.get("tool_args") or {}),
                        "tool_policy_json": Jsonb(approval.get("policy") or {}),
                        "snapshot_json": Jsonb(snapshot),
                        "requested_at": now,
                        "expires_at": expires_at,
                    },
                )
            conn.commit()
        record = {**approval, "approval_id": approval_id, "status": APPROVAL_PENDING}
        record["requested_at"] = isoformat(now)
        record["expires_at"] = isoformat(expires_at)
        record["snapshot"] = snapshot
        return approval_response(record)

    def get(self, approval_id: str) -> dict[str, Any] | None:
        self.ensure_schema()
        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT approval_id, run_id, session_id, trace_id, agent_id, release_id,
                           actor_id, channel, tool_id, runner_tool_id, runner_name,
                           tool_args_json, tool_policy_json, snapshot_json, status,
                           requested_at, expires_at, decided_at, decided_by,
                           decision_comment, result_json
                    FROM runner_approval_requests
                    WHERE approval_id = %s
                    """,
                    (approval_id,),
                )
                row = cur.fetchone()
        return row_to_record(dict(row)) if row else None

    def decide(self, approval_id: str, decision: ApprovalDecision) -> dict[str, Any]:
        self.ensure_schema()
        record = self.get(approval_id)
        if not record:
            raise ApprovalError("审批请求不存在")
        if record["status"] != APPROVAL_PENDING:
            raise ApprovalError("审批请求已处理")
        if is_expired(record):
            self._set_status(approval_id, "expired")
            raise ApprovalError("审批请求已过期")
        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE runner_approval_requests
                    SET status = %s,
                        decided_at = %s,
                        decided_by = %s,
                        decision_comment = %s
                    WHERE approval_id = %s
                    """,
                    (decision.decision, utc_now(), decision.decided_by, decision.comment, approval_id),
                )
            conn.commit()
        updated = self.get(approval_id)
        if not updated:
            raise ApprovalError("审批请求不存在")
        return updated

    def mark_executed(self, approval_id: str, result: dict[str, Any]) -> None:
        self.ensure_schema()
        from psycopg.types.json import Jsonb
        payload = json_safe(result)

        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE runner_approval_requests
                    SET status = 'executed', result_json = %s
                    WHERE approval_id = %s
                    """,
                    (Jsonb(payload), approval_id),
                )
            conn.commit()

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS runner_approval_requests (
                        approval_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        trace_id TEXT,
                        agent_id TEXT NOT NULL,
                        release_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        tool_id TEXT NOT NULL,
                        runner_tool_id TEXT,
                        runner_name TEXT,
                        tool_args_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        tool_policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        snapshot_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                        status TEXT NOT NULL DEFAULT 'pending',
                        requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        expires_at TIMESTAMPTZ,
                        decided_at TIMESTAMPTZ,
                        decided_by TEXT,
                        decision_comment TEXT,
                        result_json JSONB
                    )
                    """
                )
            conn.commit()
        self._schema_ready = True

    def _set_status(self, approval_id: str, status: str) -> None:
        with runtime_context.connect_agent_platform() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE runner_approval_requests SET status = %s WHERE approval_id = %s",
                    (status, approval_id),
                )
            conn.commit()


def row_to_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": row["approval_id"],
        "run_id": row["run_id"],
        "session_id": row["session_id"],
        "trace_id": row["trace_id"],
        "agent_id": row["agent_id"],
        "release_id": row["release_id"],
        "actor_id": row["actor_id"],
        "channel": row["channel"],
        "tool_id": row["tool_id"],
        "runner_tool_id": row["runner_tool_id"],
        "runner_name": row["runner_name"],
        "tool_args": dict(row["tool_args_json"] or {}),
        "policy": dict(row["tool_policy_json"] or {}),
        "snapshot": dict(row["snapshot_json"] or {}),
        "status": row["status"],
        "requested_at": isoformat(row["requested_at"]),
        "expires_at": isoformat(row["expires_at"]) if row.get("expires_at") else None,
        "decided_at": isoformat(row["decided_at"]) if row.get("decided_at") else None,
        "decided_by": row["decided_by"],
        "decision_comment": row["decision_comment"],
        "result": row.get("result_json"),
    }


def approval_response(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "approval_id": record["approval_id"],
        "run_id": record.get("run_id"),
        "session_id": record.get("session_id"),
        "trace_id": record.get("trace_id"),
        "agent_id": record.get("agent_id"),
        "release_id": record.get("release_id"),
        "actor_id": record.get("actor_id"),
        "channel": record.get("channel"),
        "tool_id": record.get("tool_id"),
        "runner_tool_id": record.get("runner_tool_id"),
        "runner_name": record.get("runner_name"),
        "tool_args_preview": redact_sensitive(record.get("tool_args") or {}),
        "policy": record.get("policy") or {},
        "status": record.get("status"),
        "requested_at": record.get("requested_at"),
        "expires_at": record.get("expires_at"),
    }


def redact_sensitive(value: dict[str, Any]) -> dict[str, Any]:
    hidden = {"password", "token", "api_key", "secret"}
    return {key: "***" if key.lower() in hidden else item for key, item in value.items()}


def is_expired(record: dict[str, Any]) -> bool:
    expires_at = record.get("expires_at")
    if not expires_at:
        return False
    if isinstance(expires_at, str):
        expires_at = datetime.fromisoformat(expires_at)
    return utc_now() >= expires_at


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def json_safe(value: Any) -> Any:
    # 审批结果来自 flow state，可能含有 AgentRequest 等运行时对象；入库前统一降级为 JSON。
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


default_approval_store = PostgresApprovalStore()
