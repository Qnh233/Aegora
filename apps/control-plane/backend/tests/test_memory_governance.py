from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import api


def admin_headers(monkeypatch):
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_admin")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: True)
    return {"Authorization": "Bearer admin-token"}


def user_headers(monkeypatch, user_id="u_1"):
    monkeypatch.setattr(api.db, "get_session_user", lambda _: user_id)
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    return {"Authorization": "Bearer user-token"}


def test_admin_can_force_tenant_memory_namespace_sharing(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)

    def upsert(tenant_id, namespace, *, mode, allow_public_agents, updated_by):
        captured.update(
            {
                "tenant_id": tenant_id,
                "namespace": namespace,
                "mode": mode,
                "allow_public_agents": allow_public_agents,
                "updated_by": updated_by,
            }
        )
        return {
            **captured,
            "updated_at": datetime(2026, 9, 26, tzinfo=timezone.utc),
        }

    monkeypatch.setattr(api.db, "upsert_memory_namespace_policy", upsert)

    response = TestClient(api.app).put(
        "/admin/memory-policies/default/org_profile",
        json={"mode": "tenant_required", "allow_public_agents": True},
        headers=admin_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["mode"] == "tenant_required"
    assert response.json()["allow_public_agents"] is True
    assert captured["updated_by"] == "u_admin"


def test_non_admin_cannot_change_tenant_memory_policy(monkeypatch) -> None:
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(api.db, "get_session_user", lambda _: "u_1")
    monkeypatch.setattr(api.db, "user_is_active", lambda _: True)
    monkeypatch.setattr(api.db, "user_has_role", lambda *_: False)

    response = TestClient(api.app).put(
        "/admin/memory-policies/default/org_profile",
        json={"mode": "tenant_required", "allow_public_agents": False},
        headers={"Authorization": "Bearer user-token"},
    )

    assert response.status_code == 403


def test_user_can_opt_in_to_cross_agent_memory_sharing(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)

    def upsert(tenant_id, user_id, namespace, *, share_across_agents):
        captured.update(
            {
                "tenant_id": tenant_id,
                "user_id": user_id,
                "namespace": namespace,
                "share_across_agents": share_across_agents,
            }
        )
        return {
            **captured,
            "updated_at": datetime(2026, 9, 26, tzinfo=timezone.utc),
        }

    monkeypatch.setattr(api.db, "upsert_user_memory_preference", upsert)

    response = TestClient(api.app).put(
        "/users/u_1/memory-preferences/default/preferences",
        json={"share_across_agents": True},
        headers=user_headers(monkeypatch),
    )

    assert response.status_code == 200
    assert response.json()["share_across_agents"] is True
    assert captured == {
        "tenant_id": "default",
        "user_id": "u_1",
        "namespace": "preferences",
        "share_across_agents": True,
    }


def test_user_cannot_change_another_users_memory_preference(monkeypatch) -> None:
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)

    response = TestClient(api.app).put(
        "/users/u_2/memory-preferences/default/preferences",
        json={"share_across_agents": True},
        headers=user_headers(monkeypatch, user_id="u_1"),
    )

    assert response.status_code == 403
