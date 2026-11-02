"""Google OAuth (Gmail + Calendar). Only the client id/secret and refresh token are stored, in the keyring.

Short entries on purpose: Windows Credential Manager caps each secret's size, and a full token JSON
can exceed it. The access token is refreshed in memory on startup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jarvis.secrets import SecretStore

SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"
CLIENT_ID = "GOOGLE_CLIENT_ID"
CLIENT_SECRET = "GOOGLE_CLIENT_SECRET"
REFRESH_TOKEN = "GOOGLE_REFRESH_TOKEN"


class GoogleNotConnected(RuntimeError):
    pass


def login(client_secret_file: Path, secrets: SecretStore, port: int = 0, open_browser: bool = True) -> None:
    """Run the browser consent flow once and keep only what is needed to refresh access.

    In the container there is no browser: pass a fixed *port* (published on the host's 127.0.0.1) and
    open_browser=False; the owner opens the printed link on their PC, signed in with Jarvis's own Google account.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    config = json.loads(client_secret_file.read_text(encoding="utf-8"))
    installed = config.get("installed")
    if not installed:
        raise ValueError("Ese JSON no es de tipo 'Aplicación de escritorio'. Crea un cliente OAuth de escritorio.")
    flow = InstalledAppFlow.from_client_config(config, list(SCOPES))
    creds = flow.run_local_server(
        port=port,
        open_browser=open_browser,
        bind_addr="0.0.0.0" if not open_browser else None,
        prompt="consent",
        access_type="offline",
    )
    if not creds.refresh_token:
        raise RuntimeError("Google no devolvió refresh token; revoca el acceso de Jarvis en tu cuenta y repite.")
    secrets.set(CLIENT_ID, installed["client_id"])
    secrets.set(CLIENT_SECRET, installed["client_secret"])
    secrets.set(REFRESH_TOKEN, creds.refresh_token)


def is_connected(secrets: SecretStore) -> bool:
    return all(secrets.get(n) for n in (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN))


def credentials(secrets: SecretStore) -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not is_connected(secrets):
        raise GoogleNotConnected(
            "Google no está conectado. Ejecuta: python -m jarvis google login <client_secret.json>"
        )
    creds = Credentials(
        token=None,
        refresh_token=secrets.get(REFRESH_TOKEN),
        token_uri=TOKEN_URI,
        client_id=secrets.get(CLIENT_ID),
        client_secret=secrets.get(CLIENT_SECRET),
        scopes=list(SCOPES),
    )
    creds.refresh(Request())
    return creds


def build_services(secrets: SecretStore) -> tuple[Any, Any]:
    from googleapiclient.discovery import build

    creds = credentials(secrets)
    gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
    calendar = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return gmail, calendar
