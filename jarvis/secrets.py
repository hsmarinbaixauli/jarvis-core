"""Secrets in the OS keyring (Windows Credential Manager), or in a private folder inside the Docker container.

Values are never printed or logged.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Protocol

SERVICE_NAME = "jarvis"
_INDEX_KEY = "__index__"
_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")

KNOWN_SECRETS: dict[str, str] = {
    "ANTHROPIC_API_KEY": "Clave de la API de Anthropic (console.anthropic.com)",
    "TELEGRAM_BOT_TOKEN": "Token del bot de Telegram (lo da @BotFather)",
    "OPENWEATHER_API_KEY": "Clave de OpenWeatherMap (opcional)",
    "GITHUB_TOKEN": "Token fine-grained de GitHub (fase 2)",
    "GOOGLE_PLACES_API_KEY": "Clave de Google Places (playbooks que buscan negocios)",
}


class KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class SecretError(ValueError):
    pass


def validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise SecretError("El nombre debe ir en MAYÚSCULAS con guiones bajos, p. ej. TELEGRAM_BOT_TOKEN.")
    return name


class FileSecretBackend:
    """One file per secret in a folder only Jarvis's user can read (0700/0600).

    Used in the container, where there is no OS keyring: the folder is a Docker volume mounted only into
    Jarvis's container, never into the browser's, and never a folder on the owner's disk.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            directory.chmod(0o700)

    def _path(self, service: str, username: str) -> Path:
        if not re.match(r"^[A-Za-z0-9_]+$", service) or not re.match(r"^[A-Za-z0-9_]+$", username):
            raise SecretError("Nombre de clave no válido.")
        return self.directory / f"{service}.{username}"

    def get_password(self, service: str, username: str) -> str | None:
        path = self._path(service, username)
        return path.read_text(encoding="utf-8") if path.is_file() else None

    def set_password(self, service: str, username: str, password: str) -> None:
        path = self._path(service, username)
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(password)
        os.replace(tmp, path)

    def delete_password(self, service: str, username: str) -> None:
        self._path(service, username).unlink(missing_ok=True)


SECRETS_DIR_ENV = "JARVIS_SECRETS_DIR"


def _default_backend() -> KeyringBackend:
    directory = os.environ.get(SECRETS_DIR_ENV)
    if directory:
        return FileSecretBackend(Path(directory))
    import keyring

    return keyring.get_keyring()


class SecretStore:
    def __init__(self, backend: KeyringBackend | None = None, service: str = SERVICE_NAME) -> None:
        self._backend: Any = backend if backend is not None else _default_backend()
        self._service = service

    def get(self, name: str) -> str | None:
        return self._backend.get_password(self._service, validate_name(name))

    def set(self, name: str, value: str) -> None:
        value = value.strip()
        if not value:
            raise SecretError("El valor está vacío; no se ha guardado nada.")
        self._backend.set_password(self._service, validate_name(name), value)
        names = set(self.names())
        names.add(name)
        self._write_index(names)

    def delete(self, name: str) -> bool:
        validate_name(name)
        existed = self.get(name) is not None
        if existed:
            self._backend.delete_password(self._service, name)
        self._write_index(set(self.names()) - {name})
        return existed

    def names(self) -> list[str]:
        raw = self._backend.get_password(self._service, _INDEX_KEY)
        if not raw:
            return []
        try:
            return sorted(n for n in json.loads(raw) if isinstance(n, str))
        except json.JSONDecodeError:
            return []

    def values(self) -> list[str]:
        return [v for n in self.names() if (v := self.get(n))]

    def _write_index(self, names: set[str]) -> None:
        self._backend.set_password(self._service, _INDEX_KEY, json.dumps(sorted(names)))
