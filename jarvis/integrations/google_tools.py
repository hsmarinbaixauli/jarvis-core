"""Gmail and Calendar as in-process MCP tools. Reading is free; sending always waits for the owner's ✅."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any

from callgate import Effect, Tier, ToolRegistry, ToolSpec

SERVER_NAME = "google"
_MAX_BODY_CHARS = 15_000
_EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[a-zA-Z]{2,}$")

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("gmail_list_unread", Effect.READ, "Leer la bandeja de entrada."),
    ToolSpec("gmail_read", Effect.READ, "Leer un correo."),
    ToolSpec("gmail_mark_read", Effect.WRITE, "Marcar un correo como leído.", tier=Tier.FREE, external=False),
    ToolSpec("gmail_send_reply", Effect.IRREVERSIBLE, "Enviar una respuesta por correo.", external=False),
    ToolSpec("gmail_send_email", Effect.IRREVERSIBLE, "Enviar un correo nuevo.", external=False),
    ToolSpec("calendar_list", Effect.READ, "Consultar el calendario."),
    ToolSpec(
        "calendar_create_event", Effect.WRITE, "Crear un evento en tu calendario (sin invitados).", external=False
    ),
)


def wrap_external(source: str, payload: Any) -> str:
    """Mark third-party content so the model treats it as data (see the system prompt)."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    text = text.replace("</contenido_externo", "&lt;/contenido_externo")
    return f'<contenido_externo origen="{source}">\n{text}\n</contenido_externo>'


def _text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _error(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _parse_dt(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.strip())
    return value if value.tzinfo else value.astimezone()


def send_new_email(service: Any, to: str, subject: str, body: str) -> dict[str, Any]:
    import base64
    import email.message

    msg = email.message.EmailMessage()
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def build_tools(services: Callable[[], tuple[Any, Any]]) -> list[Any]:
    """*services* returns (gmail, calendar) lazily, so Jarvis starts even if Google is down."""
    from claude_agent_sdk import tool

    from jarvis.gcalendar.events import create_event, get_upcoming_events
    from jarvis.gmail.messages import get_message_full, get_unread_messages, mark_as_read, send_reply

    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    @tool(
        "gmail_list_unread", "Lista los correos no leídos de Gmail (remitente, asunto, extracto).", {"max_results": int}
    )
    async def gmail_list_unread(args: dict[str, Any]) -> dict[str, Any]:
        gmail, _ = services()
        limit = max(1, min(int(args.get("max_results") or 10), 25))
        return _text(wrap_external("gmail", await run(get_unread_messages, gmail, limit)))

    @tool("gmail_read", "Lee el contenido completo de un correo por su id.", {"message_id": str})
    async def gmail_read(args: dict[str, Any]) -> dict[str, Any]:
        gmail, _ = services()
        message = await run(get_message_full, gmail, str(args["message_id"]))
        message["body_plain"] = (message.get("body_plain") or "")[:_MAX_BODY_CHARS]
        return _text(wrap_external("gmail", message))

    @tool("gmail_mark_read", "Marca un correo como leído.", {"message_id": str})
    async def gmail_mark_read(args: dict[str, Any]) -> dict[str, Any]:
        gmail, _ = services()
        await run(mark_as_read, gmail, str(args["message_id"]))
        return _text("Marcado como leído.")

    @tool(
        "gmail_send_reply",
        "Responde a un correo en el mismo hilo. Requiere la aprobación del usuario.",
        {"message_id": str, "body": str},
    )
    async def gmail_send_reply(args: dict[str, Any]) -> dict[str, Any]:
        gmail, _ = services()
        body = str(args.get("body", "")).strip()
        if not body:
            return _error("El cuerpo de la respuesta está vacío.")
        await run(send_reply, gmail, str(args["message_id"]), body)
        return _text("Respuesta enviada.")

    @tool(
        "gmail_send_email",
        "Envía un correo nuevo a una dirección. Requiere la aprobación del usuario.",
        {"to": str, "subject": str, "body": str},
    )
    async def gmail_send_email(args: dict[str, Any]) -> dict[str, Any]:
        gmail, _ = services()
        to = str(args.get("to", "")).strip()
        if not _EMAIL_RE.match(to):
            return _error(f"Dirección no válida: {to!r}. Solo un destinatario por correo.")
        await run(send_new_email, gmail, to, str(args.get("subject", "")).strip(), str(args.get("body", "")))
        return _text(f"Correo enviado a {to}.")

    @tool("calendar_list", "Lista los eventos del calendario de hoy y los próximos días (1–31).", {"days": int})
    async def calendar_list(args: dict[str, Any]) -> dict[str, Any]:
        _, calendar = services()
        days = max(1, min(int(args.get("days") or 7), 31))
        events = await run(get_upcoming_events, calendar, days)
        slim = [
            {k: e.get(k) for k in ("id", "summary", "start", "end", "location", "description", "hangoutLink")}
            for e in events
        ]
        return _text(wrap_external("calendar", slim))

    @tool(
        "calendar_create_event",
        "Crea un evento en el calendario del usuario. Fechas en ISO 8601 (2026-11-03T10:00). No invita a nadie.",
        {"title": str, "start": str, "end": str, "description": str},
    )
    async def calendar_create_event(args: dict[str, Any]) -> dict[str, Any]:
        _, calendar = services()
        try:
            start, end = _parse_dt(str(args["start"])), _parse_dt(str(args["end"]))
        except (KeyError, ValueError):
            return _error("Fechas no válidas; usa formato ISO 8601, p. ej. 2026-11-03T10:00.")
        event = await run(
            create_event, calendar, str(args.get("title", "")), start, end, str(args.get("description", ""))
        )
        return _text(f"Evento creado: {event.get('htmlLink', event.get('id', ''))}")

    return [
        gmail_list_unread,
        gmail_read,
        gmail_mark_read,
        gmail_send_reply,
        gmail_send_email,
        calendar_list,
        calendar_create_event,
    ]


def build_server(services: Callable[[], tuple[Any, Any]], registry: ToolRegistry) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server

    tools = build_tools(services)
    registry.require(SERVER_NAME, [t.name for t in tools])
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
