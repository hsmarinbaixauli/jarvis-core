"""Local web chat on 127.0.0.1.

Loopback is not authentication: any page open in the owner's browser can send requests to localhost.
So every request needs the session cookie (set from the one-time link printed at startup), the
Host header must be loopback (blocks DNS rebinding) and POSTs must come from our own origin.
"""

from __future__ import annotations

import hmac
import os
import secrets
from collections import deque
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jarvis.core.events import Event
from jarvis.core.service import Halted, JarvisCore

STATIC_DIR = Path(__file__).resolve().parent / "static"
COOKIE_NAME = "jarvis_session"
_NO_SESSION_PAGE = """<!doctype html><meta charset="utf-8"><title>Jarvis</title>
<body style="font-family:system-ui;max-width:36rem;margin:4rem auto;padding:0 1rem">
<h1>Jarvis</h1><p>Abre el enlace que aparece en la consola al arrancar Jarvis,
o ejecútalo de nuevo con <code>python -m jarvis web-url</code>.</p></body>"""


CHANNEL = "web"
_CHAT_WAIT_S = 15 * 60


class WebChannel:
    """The web never gets approvals (those need the owner's phone); it keeps recent events for the dashboard."""

    name = CHANNEL
    can_approve = False

    def __init__(self, keep: int = 200) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=keep)

    async def deliver(self, event: Event) -> None:
        if event.kind in ("notice", "task"):
            self.events.append(
                {"kind": event.kind, "text": event.text, "change": event.extra.get("change"), "task": event.task}
            )


def _task_view(task: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "kind", "label", "status", "attempts", "max_attempts", "cost_usd", "created_at", "finished_at")
    return {k: task[k] for k in keys} | {"error": task["error"]}


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = Field(default=None, max_length=64)


def load_or_create_token(data_dir: Path) -> str:
    path = data_dir / "web_token"
    if path.is_file():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o600)
    return token


def create_web_app(
    *,
    core: JarvisCore,
    token: str,
    port: int,
    channel: WebChannel | None = None,
    extra_allowed_hosts: tuple[str, ...] = (),
) -> FastAPI:
    store = core.store
    if channel is None:
        channel = WebChannel()
        core.register(channel)
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None)
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", *extra_allowed_hosts}
    allowed_origins = {f"http://{h}" for h in allowed_hosts}

    def _authorised(request: Request) -> bool:
        cookie = request.cookies.get(COOKIE_NAME, "")
        return bool(cookie) and hmac.compare_digest(cookie, token)

    @app.middleware("http")
    async def _guard(request: Request, call_next: Any) -> Response:
        if request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Host no permitido"}, status_code=400)
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin is None or origin not in allowed_origins:
                return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
        response: Response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def _require_auth(request: Request) -> None:
        if not _authorised(request):
            raise HTTPException(status_code=401, detail="Sesión no válida")

    @app.get("/", response_model=None)
    async def index(request: Request, t: str | None = None) -> Response:
        if t is not None:
            if not hmac.compare_digest(t, token):
                return HTMLResponse(_NO_SESSION_PAGE, status_code=401)
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 90)
            return response
        if not _authorised(request):
            return HTMLResponse(_NO_SESSION_PAGE, status_code=401)
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/conversations")
    async def conversations(request: Request) -> list[dict[str, Any]]:
        _require_auth(request)
        return [
            {
                "id": c["id"],
                "title": c["title"] or "Sin título",
                "channel": c["channel"],
                "updated_at": c["updated_at"],
            }
            for c in store.list_conversations()
        ]

    @app.get("/api/conversations/{conversation_id}/messages")
    async def messages(request: Request, conversation_id: str) -> list[dict[str, Any]]:
        _require_auth(request)
        if store.get_conversation(conversation_id) is None:
            raise HTTPException(status_code=404, detail="Conversación no encontrada")
        return store.get_messages(conversation_id)

    @app.post("/api/chat")
    async def chat(request: Request, body: ChatIn) -> dict[str, Any]:
        _require_auth(request)
        conversation_id = body.conversation_id
        if conversation_id is None or store.get_conversation(conversation_id) is None:
            conversation_id = store.create_conversation(CHANNEL)
        try:
            task = await core.ask(CHANNEL, body.message, conversation_id)
        except Halted as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            task = await core.wait(task["id"], timeout=_CHAT_WAIT_S)
        except TimeoutError:
            return {
                "conversation_id": conversation_id,
                "task_id": task["id"],
                "reply": "Sigue en marcha.",
                "status": "running",
            }
        reply = task["result"] if task["status"] == "done" else f"⚠️ {task['error'] or task['status']}"
        return {"conversation_id": conversation_id, "task_id": task["id"], "reply": reply, "status": task["status"]}

    @app.get("/api/status")
    async def status(request: Request) -> dict[str, Any]:
        _require_auth(request)
        budget = core.budget
        return {
            "halted": core.halted,
            "spent_today_usd": budget.spent_today() if budget else None,
            "daily_budget_usd": budget.daily_usd if budget else None,
            "running": len(store.list_tasks(("running",))),
            "queued": len(store.list_tasks(("queued",))),
            "pending_approvals": len(core.broker.pending()),
        }

    @app.get("/api/tasks")
    async def tasks(request: Request, limit: int = 50) -> list[dict[str, Any]]:
        _require_auth(request)
        return [_task_view(t) for t in store.list_tasks(limit=max(1, min(limit, 200)))]

    @app.get("/api/tasks/{task_id}")
    async def task_detail(request: Request, task_id: int) -> dict[str, Any]:
        _require_auth(request)
        task = store.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Tarea no encontrada")
        return _task_view(task) | {"result": task["result"]}

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(request: Request, task_id: int) -> dict[str, Any]:
        _require_auth(request)
        return {"cancelled": await core.queue.cancel(task_id)}

    @app.get("/api/events")
    async def events(request: Request) -> list[dict[str, Any]]:
        _require_auth(request)
        return list(channel.events)

    @app.post("/api/stop")
    async def stop(request: Request) -> dict[str, Any]:
        _require_auth(request)
        return {"message": await core.halt(CHANNEL), "halted": core.halted}

    @app.post("/api/resume")
    async def resume(request: Request) -> dict[str, Any]:
        _require_auth(request)
        return {"message": await core.resume(), "halted": core.halted}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
