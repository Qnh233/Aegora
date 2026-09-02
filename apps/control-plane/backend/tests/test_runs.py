from fastapi.testclient import TestClient

from app import api


def stub_dependencies(monkeypatch):
    events = []
    monkeypatch.setattr(api.db, "ensure_schema", lambda: None)
    monkeypatch.setattr(
        api.db,
        "create_run",
        lambda run_id, message: events.append(("created", run_id, message)),
    )
    monkeypatch.setattr(
        api.db,
        "update_run",
        lambda run_id, status, answer=None, error=None: events.append(
            (status, run_id, answer, error)
        ),
    )
    return events


def test_run_returns_llm_reply_and_records_audit(monkeypatch):
    events = stub_dependencies(monkeypatch)
    monkeypatch.setattr(api.runner, "call_llm", lambda message: f"reply: {message}")

    response = TestClient(api.app).post("/runs", json={"message": "hello"})

    assert response.status_code == 200
    assert response.json()["answer"] == "reply: hello"
    assert events[0][0] == "created"
    assert events[1][0] == "succeeded"


def test_run_rejects_blank_message(monkeypatch):
    stub_dependencies(monkeypatch)

    response = TestClient(api.app).post("/runs", json={"message": "   "})

    assert response.status_code == 422
    assert response.json()["detail"] == "message 不能为空"


def test_llm_failure_is_recorded(monkeypatch):
    events = stub_dependencies(monkeypatch)

    def fail(_: str):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(api.runner, "call_llm", fail)
    response = TestClient(api.app).post("/runs", json={"message": "hello"})

    assert response.status_code == 502
    assert events[-1][0] == "failed"
    assert "provider unavailable" in events[-1][3]
