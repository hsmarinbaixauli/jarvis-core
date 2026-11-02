"""Jarvis's own Obsidian vault: a folder of Markdown notes, read and written as in-process MCP tools.

It is a separate vault, never the owner's personal one. No Obsidian app or plugin is needed: the vault is
just files. Jarvis writes freely (with a notice) only under ``Jarvis/``; changing any other note, which the
owner wrote, needs approval. Paths are confined to the vault and only ``.md`` files are touched.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from callgate import Effect, Tier, ToolRegistry, ToolSpec

SERVER_NAME = "vault"
JARVIS_FOLDER = "Jarvis"
_HIDDEN = {".obsidian", ".git", ".trash"}
_MAX_READ_CHARS = 30_000
_MAX_WRITE_CHARS = 50_000
_MAX_RESULTS = 20
_SNIPPET = 160

# Notes Jarvis wrote may carry what it read on the web, so reading the vault taints like any external content.
SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("vault_search", Effect.READ, "Buscar en el vault de Jarvis."),
    ToolSpec("vault_list", Effect.READ, "Listar notas del vault de Jarvis."),
    ToolSpec("vault_read", Effect.READ, "Leer una nota del vault de Jarvis."),
    ToolSpec("vault_create", Effect.WRITE, "Crear una nota en Jarvis/ del vault.", external=False),
    ToolSpec("vault_append", Effect.WRITE, "Añadir texto a una nota de Jarvis/ del vault.", external=False),
    ToolSpec(
        "vault_edit",
        Effect.WRITE,
        "Jarvis quiere cambiar una nota del vault que no escribió él.",
        tier=Tier.APPROVAL,
        external=False,
    ),
)


class VaultError(ValueError):
    pass


class Vault:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def ensure(self) -> None:
        (self.root / JARVIS_FOLDER).mkdir(parents=True, exist_ok=True)

    def resolve(self, relative: str) -> Path:
        """A note path inside the vault, or VaultError. Accepts "Carpeta/Nota" with or without ".md"."""
        raw = str(relative).strip().replace("\\", "/")
        if not raw:
            raise VaultError("Falta la ruta de la nota.")
        posix = PurePosixPath(raw)
        if posix.is_absolute() or re.match(r"^[A-Za-z]:", raw) or ".." in posix.parts:
            raise VaultError("Usa una ruta relativa dentro del vault, sin '..'.")
        if any(part in _HIDDEN or part.startswith(".") for part in posix.parts):
            raise VaultError("Esa carpeta del vault no se toca.")
        if posix.suffix.lower() != ".md":
            posix = posix.with_name(posix.name + ".md")
        path = (self.root / posix).resolve()
        if not path.is_relative_to(self.root):
            raise VaultError("Esa ruta sale del vault.")
        return path

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def is_jarvis_note(self, path: Path) -> bool:
        return path.is_relative_to(self.root / JARVIS_FOLDER)

    def notes(self, folder: str = "") -> list[Path]:
        base = self.root
        if folder.strip():
            base = self.resolve(folder.rstrip("/") + "/x.md").parent
        if not base.is_dir():
            return []
        found = []
        for path in base.rglob("*.md"):
            parts = path.relative_to(self.root).parts
            if any(part in _HIDDEN or part.startswith(".") for part in parts):
                continue
            if path.is_file() and path.resolve().is_relative_to(self.root):
                found.append(path)
        return sorted(found)

    def search(self, query: str) -> list[dict[str, str]]:
        words = [w.lower() for w in query.split() if w.strip()]
        if not words:
            raise VaultError("Escribe qué buscar.")
        hits: list[tuple[int, dict[str, str]]] = []
        for path in self.notes():
            text = path.read_text(encoding="utf-8", errors="replace")
            haystack = f"{path.stem}\n{text}".lower()
            if not all(w in haystack for w in words):
                continue
            score = sum(3 for w in words if w in path.stem.lower()) + sum(haystack.count(w) for w in words)
            at = text.lower().find(words[0])
            start = max(0, at - _SNIPPET // 2) if at >= 0 else 0
            snippet = " ".join(text[start : start + _SNIPPET].split())
            hits.append((score, {"note": self.relative(path), "snippet": snippet}))
        hits.sort(key=lambda h: (-h[0], h[1]["note"]))
        return [h for _, h in hits[:_MAX_RESULTS]]

    def read(self, relative: str) -> str:
        path = self.resolve(relative)
        if not path.is_file():
            raise VaultError(f"No existe la nota {self.relative(path)}.")
        text = path.read_text(encoding="utf-8", errors="replace")
        return text if len(text) <= _MAX_READ_CHARS else text[:_MAX_READ_CHARS] + "\n[…recortado]"

    def _jarvis_path(self, relative: str) -> Path:
        raw = str(relative).strip().replace("\\", "/")
        if not raw.split("/", 1)[0] == JARVIS_FOLDER:
            raw = f"{JARVIS_FOLDER}/{raw}"
        path = self.resolve(raw)
        if not self.is_jarvis_note(path):
            raise VaultError(f"Sin aprobación solo escribo dentro de {JARVIS_FOLDER}/; para otras notas, vault_edit.")
        return path

    def create(self, relative: str, content: str) -> str:
        path = self._jarvis_path(relative)
        _check_size(content)
        if path.exists():
            raise VaultError(f"Ya existe {self.relative(path)}: usa vault_append para añadir.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        return self.relative(path)

    def append(self, relative: str, content: str, when: datetime | None = None) -> str:
        path = self._jarvis_path(relative)
        _check_size(content)
        if not path.is_file():
            raise VaultError(f"No existe {self.relative(path)}: créala con vault_create.")
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        text = path.read_text(encoding="utf-8").rstrip("\n")
        path.write_text(f"{text}\n\n<!-- {stamp} -->\n{content.strip()}\n", encoding="utf-8")
        return self.relative(path)

    def edit(self, relative: str, content: str) -> str:
        path = self.resolve(relative)
        _check_size(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip("\n") + "\n", encoding="utf-8")
        return self.relative(path)


def _check_size(content: str) -> None:
    if not content.strip():
        raise VaultError("La nota está vacía.")
    if len(content) > _MAX_WRITE_CHARS:
        raise VaultError(f"La nota es demasiado larga (máximo {_MAX_WRITE_CHARS} caracteres).")


def _text(payload: Any) -> dict[str, Any]:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=1)
    return {"content": [{"type": "text", "text": text}]}


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def build_tools(vault: Vault) -> list[Any]:
    from claude_agent_sdk import tool

    def guarded(fn: Any) -> Any:
        async def run(args: dict[str, Any]) -> dict[str, Any]:
            try:
                return _text(fn(args))
            except (VaultError, OSError) as exc:
                return _error(str(exc))

        return run

    vault_search = tool(
        "vault_search",
        "Busca notas del vault de Jarvis que contengan todas las palabras (título o texto).",
        {"query": str},
    )(guarded(lambda a: vault.search(str(a.get("query", ""))) or "Ninguna nota coincide."))
    vault_list = tool(
        "vault_list",
        "Lista las notas del vault (o de una carpeta suya, p. ej. 'Jarvis/Empresas').",
        {"folder": str},
    )(guarded(lambda a: [vault.relative(p) for p in vault.notes(str(a.get("folder", "")))][:200] or "No hay notas."))
    vault_read = tool("vault_read", "Lee una nota del vault por su ruta (p. ej. 'Jarvis/Diario').", {"note": str})(
        guarded(lambda a: vault.read(str(a.get("note", ""))))
    )
    vault_create = tool(
        "vault_create",
        "Crea una nota nueva en la carpeta Jarvis/ del vault. Usa [[wikilinks]] para enlazar notas.",
        {"note": str, "content": str},
    )(guarded(lambda a: f"Creada {vault.create(str(a.get('note', '')), str(a.get('content', '')))}."))
    vault_append = tool(
        "vault_append",
        "Añade texto al final de una nota que ya existe en la carpeta Jarvis/ del vault.",
        {"note": str, "content": str},
    )(guarded(lambda a: f"Añadido a {vault.append(str(a.get('note', '')), str(a.get('content', '')))}."))
    vault_edit = tool(
        "vault_edit",
        "Reemplaza el contenido completo de cualquier nota del vault (pide aprobación). Para notas de Jarvis/ "
        "usa mejor vault_create o vault_append.",
        {"note": str, "content": str},
    )(guarded(lambda a: f"Guardada {vault.edit(str(a.get('note', '')), str(a.get('content', '')))}."))
    return [vault_search, vault_list, vault_read, vault_create, vault_append, vault_edit]


def build_server(vault: Vault, registry: ToolRegistry) -> Any:
    from claude_agent_sdk import create_sdk_mcp_server

    tools = build_tools(vault)
    registry.require(SERVER_NAME, [t.name for t in tools])
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)
