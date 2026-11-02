"""Long-term memory: the owner's notes file (memory/memoria.md), contacts and to-dos as in-process MCP tools.

The agent cannot write the notes file directly (it is a protected path). It can only propose a note
with memory_propose_note, which is an approval-tier tool: the note is appended only after ✅.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from callgate import Effect, Tier, ToolRegistry, ToolSpec

from jarvis.store import Store

SERVER_NAME = "memory"
_MAX_MEMORY_CHARS = 12_000
_NOTES_HEADER = "## Notas añadidas por Jarvis"

# The owner's own data does not taint the conversation. Contacts can be saved from emails, so they do.
SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "memory_propose_note",
        Effect.WRITE,
        "Jarvis quiere apuntar esto en tu memoria.",
        tier=Tier.APPROVAL,
        external=False,
    ),
    ToolSpec("contacts_search", Effect.READ, "Buscar en tus contactos."),
    ToolSpec("contacts_save", Effect.WRITE, "Guardar o actualizar un contacto.", external=False),
    ToolSpec("todos_list", Effect.READ, "Ver tus pendientes.", external=False),
    ToolSpec("todos_add", Effect.WRITE, "Añadir un pendiente.", tier=Tier.FREE, external=False),
    ToolSpec("todos_complete", Effect.WRITE, "Marcar un pendiente como hecho.", tier=Tier.FREE, external=False),
)

TEMPLATE = """# Memoria

Este archivo lo lee Jarvis en cada conversación. Escribe aquí lo que quieres que sepa siempre.

## Sobre mí
- (quién eres y a qué te dedicas)

## Preferencias
- Respuestas breves y directas.

## Proyectos en marcha
-
"""


class MemoryFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure(self) -> None:
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(TEMPLATE, encoding="utf-8")

    def read(self) -> str:
        if not self.path.is_file():
            return ""
        text = self.path.read_text(encoding="utf-8")
        return text if len(text) <= _MAX_MEMORY_CHARS else text[:_MAX_MEMORY_CHARS] + "\n[…recortado]"

    def append_note(self, note: str, when: datetime | None = None) -> None:
        self.ensure()
        stamp = (when or datetime.now()).strftime("%Y-%m-%d")
        clean = " ".join(note.split())
        text = self.path.read_text(encoding="utf-8").rstrip("\n")
        if _NOTES_HEADER not in text:
            text += f"\n\n{_NOTES_HEADER}\n"
        self.path.write_text(f"{text}\n- ({stamp}) {clean}\n", encoding="utf-8")


def _text(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    return {"content": [{"type": "text", "text": text}]}


def build_tools(store: Store, memory: MemoryFile) -> list[Any]:
    from claude_agent_sdk import tool

    @tool(
        "memory_propose_note",
        "Propone apuntar un dato duradero sobre el usuario en su memoria (lo tiene que aprobar).",
        {"note": str, "reason": str},
    )
    async def memory_propose_note(args: dict[str, Any]) -> dict[str, Any]:
        note = str(args.get("note", "")).strip()
        if not note or len(note) > 500:
            return {
                "content": [{"type": "text", "text": "La nota debe tener entre 1 y 500 caracteres."}],
                "is_error": True,
            }
        memory.append_note(note)
        return _text("Anotado en la memoria.")

    @tool("contacts_search", "Busca contactos por nombre, email, empresa o notas.", {"query": str})
    async def contacts_search(args: dict[str, Any]) -> dict[str, Any]:
        return _text(store.search_contacts(str(args.get("query", ""))))

    @tool(
        "contacts_save",
        "Guarda o actualiza un contacto (por email).",
        {"name": str, "email": str, "phone": str, "company": str, "notes": str},
    )
    async def contacts_save(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", "")).strip()
        if not name:
            return {"content": [{"type": "text", "text": "Falta el nombre."}], "is_error": True}
        contact_id = store.save_contact(
            name=name,
            email=str(args.get("email", "")),
            phone=str(args.get("phone", "")),
            company=str(args.get("company", "")),
            notes=str(args.get("notes", "")),
        )
        return _text(f"Contacto guardado (#{contact_id}).")

    @tool("todos_list", "Lista los pendientes del usuario.", {"include_done": bool})
    async def todos_list(args: dict[str, Any]) -> dict[str, Any]:
        return _text(store.list_todos(bool(args.get("include_done"))))

    @tool(
        "todos_add",
        "Añade un pendiente para el usuario. 'due' opcional en formato AAAA-MM-DD.",
        {"text": str, "due": str},
    )
    async def todos_add(args: dict[str, Any]) -> dict[str, Any]:
        text = str(args.get("text", "")).strip()
        if not text:
            return {"content": [{"type": "text", "text": "El pendiente está vacío."}], "is_error": True}
        return _text(f"Pendiente #{store.add_todo(text, str(args.get('due', '')))} añadido.")

    @tool("todos_complete", "Marca un pendiente como hecho por su número.", {"id": int})
    async def todos_complete(args: dict[str, Any]) -> dict[str, Any]:
        done = store.complete_todo(int(args.get("id", 0)))
        return _text("Hecho." if done else "No encuentro ese pendiente abierto.")

    return [memory_propose_note, contacts_search, contacts_save, todos_list, todos_add, todos_complete]


def build_server(store: Store, memory: MemoryFile, registry: ToolRegistry) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server

    tools = build_tools(store, memory)
    registry.require(SERVER_NAME, [t.name for t in tools])
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
