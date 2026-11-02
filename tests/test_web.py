from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jarvis.web.app import COOKIE_NAME, create_web_app, load_or_create_token

TOKEN = "t" * 43
PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"


@pytest.fixture()
def client(core: Any) -> Iterator[TestClient]:
    app = create_web_app(core=core, token=TOKEN, port=PORT)

    @asynccontextmanager
    async def run_queue(_: Any) -> AsyncIterator[None]:
        dispatcher = asyncio.ensure_future(core.queue.run_forever())
        yield
        dispatcher.cancel()

    app.router.lifespan_context = run_queue

    with TestClient(app, base_url=ORIGIN) as test_client:
        yield test_client


def login(client: TestClient) -> None:
    response = client.get(f"/?t={TOKEN}", follow_redirects=False)
    assert response.status_code == 303
    assert "httponly" in response.headers["set-cookie"].lower()
    assert "samesite=strict" in response.headers["set-cookie"].lower()


def test_no_session_shows_help_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 401 and "web-url" in response.text


def test_wrong_token_rejected(client: TestClient) -> None:
    assert client.get("/?t=wrong", follow_redirects=False).status_code == 401


def test_login_then_index(client: TestClient) -> None:
    login(client)
    response = client.get("/")
    assert response.status_code == 200 and "Jarvis" in response.text
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_api_requires_session(client: TestClient) -> None:
    assert client.get("/api/conversations").status_code == 401
    response = client.post("/api/chat", json={"message": "hola"}, headers={"Origin": ORIGIN})
    assert response.status_code == 401


def test_dns_rebinding_host_rejected(client: TestClient) -> None:
    login(client)
    response = client.get("/api/conversations", headers={"Host": "evil.example:8765"})
    assert response.status_code == 400


def test_cross_origin_post_rejected(client: TestClient, brain_script: Any) -> None:
    login(client)
    response = client.post("/api/chat", json={"message": "hola"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403 and brain_script.calls == []
    response = client.post("/api/chat", json={"message": "hola"})
    assert response.status_code == 403


def test_chat_flow(client: TestClient, brain_script: Any, store: Any) -> None:
    login(client)
    first = client.post("/api/chat", json={"message": "hola"}, headers={"Origin": ORIGIN}).json()
    assert first["reply"] == "eco: hola"
    second = client.post(
        "/api/chat", json={"message": "otra", "conversation_id": first["conversation_id"]}, headers={"Origin": ORIGIN}
    ).json()
    assert second["conversation_id"] == first["conversation_id"]
    listing = client.get("/api/conversations").json()
    assert listing[0]["id"] == first["conversation_id"]
    assert client.get("/api/conversations/nope/messages").status_code == 404
    tasks = client.get("/api/tasks").json()
    assert [t["status"] for t in tasks] == ["done", "done"] and tasks[0]["cost_usd"] == pytest.approx(0.01)
    assert client.get(f"/api/tasks/{first['task_id']}").json()["result"] == "eco: hola"
    assert client.get("/api/tasks/999").status_code == 404
    assert any(e["kind"] == "task" for e in client.get("/api/events").json())


def test_status_stop_and_resume(client: TestClient, store: Any) -> None:
    login(client)
    assert client.get("/api/status").json()["halted"] is False
    assert client.post("/api/stop").status_code == 403  # no Origin header: blocked like any cross-site POST
    stopped = client.post("/api/stop", headers={"Origin": ORIGIN}).json()
    assert stopped["halted"] is True and "PARADA TOTAL" in stopped["message"]
    blocked = client.post("/api/chat", json={"message": "hola"}, headers={"Origin": ORIGIN})
    assert blocked.status_code == 409 and store.list_tasks() == []
    assert client.get("/api/status").json() | {"spent_today_usd": 0} == {
        "halted": True,
        "spent_today_usd": 0,
        "daily_budget_usd": 10.0,
        "running": 0,
        "queued": 0,
        "pending_approvals": 0,
    }
    assert client.post("/api/resume", headers={"Origin": ORIGIN}).json()["halted"] is False


def test_cookie_value_must_match(client: TestClient) -> None:
    client.cookies.set(COOKIE_NAME, "forged")
    assert client.get("/api/conversations").status_code == 401


def test_token_persisted_and_strong(tmp_path: Any) -> None:
    token = load_or_create_token(tmp_path)
    assert len(token) >= 32 and load_or_create_token(tmp_path) == token
