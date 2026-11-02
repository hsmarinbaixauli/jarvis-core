"""Playwright MCP ("eyes"): a real browser with a persistent profile where the owner logs in by hand.

Jarvis never sees passwords: the owner signs in once with `python -m jarvis browser login`, and the
cookies stay in the profile folder inside Jarvis's data dir (which the agent itself cannot read).

For sites that need a password typed, the owner can register credentials (keyring secret + allowed
domains). Playwright MCP's --secrets types the real value when the model passes the secret's *name*
and redacts it from every response; the gate only lets that happen on the listed domains.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from callgate import Decision, Effect, PolicyContext, Tier, ToolSpec, classify_url, domain_matches

SERVER_NAME = "playwright"
PLAYWRIGHT_MCP_PACKAGE = "@playwright/mcp@0.0.83"
_HOST_KEY = "browser_host"
_CREDENTIALS_KEY = "browser_credentials"
_INTERACT_KEY = "browser_interact_domains"
_PAGE_URL_RE = re.compile(r"Page URL:\s*(\S+)")
# After these the page may have changed without a navigate call, so the site is unknown until proven.
_MAY_NAVIGATE = frozenset(
    {
        "browser_click",
        "browser_press_key",
        "browser_select_option",
        "browser_navigate_back",
        "browser_tabs",
        "browser_type",
        "browser_fill_form",
        "browser_handle_dialog",
    }
)

_READ_ONLY = frozenset(
    {
        "browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_network_request",
        "browser_find",
        "browser_wait_for",
        "browser_tabs",
        "browser_navigate_back",
        "browser_hover",
        "browser_resize",
        "browser_close",
        "browser_emulate_media",
    }
)
# Interactions that may act on a logged-in site ("Buy" is a click): a notice only on interact_domains.
_INTERACT = frozenset({"browser_click", "browser_select_option", "browser_drag", "browser_type", "browser_press_key"})
# Hands files over or confirms dialogs ("Delete?"): cannot be taken back.
_HAND_OVER = frozenset({"browser_file_upload", "browser_handle_dialog", "browser_drop"})
# Arbitrary JavaScript in a logged-in browser is equivalent to a shell.
_NEVER = frozenset({"browser_evaluate", "browser_run_code_unsafe", "browser_install"})

SPECS: tuple[ToolSpec, ...] = (
    ToolSpec("browser_navigate", Effect.READ, "Abrir una web."),
    *(ToolSpec(n, Effect.READ, "Leer la página o mover el navegador.") for n in sorted(_READ_ONLY)),
    *(ToolSpec(n, Effect.WRITE, "Hacer clic o escribir en una web.", tier=Tier.APPROVAL) for n in sorted(_INTERACT)),
    ToolSpec("browser_fill_form", Effect.WRITE, "Rellenar un formulario.", tier=Tier.APPROVAL),
    *(
        ToolSpec(n, Effect.IRREVERSIBLE, "Enviar archivos o confirmar un diálogo en una web.")
        for n in sorted(_HAND_OVER)
    ),
    *(
        ToolSpec(n, Effect.IRREVERSIBLE, "Ejecutar código arbitrario en el navegador no está permitido.", Tier.BLOCK)
        for n in sorted(_NEVER)
    ),
)


def remote_config(url: str) -> dict[str, Any]:
    """The browser container (Docker): Playwright MCP over HTTP, with its own profile volume."""
    return {"type": "http", "url": url}


def mcp_config(
    profile_dir: Path, output_dir: Path, channel: str, headless: bool, secrets_file: Path | None = None
) -> dict[str, Any]:
    args = [
        "-y",
        PLAYWRIGHT_MCP_PACKAGE,
        "--browser",
        channel,
        "--user-data-dir",
        str(profile_dir),
        "--output-dir",
        str(output_dir),
        "--no-webmcp",
    ]
    if headless:
        args.append("--headless")
    if secrets_file is not None:
        args += ["--secrets", str(secrets_file)]
    if sys.platform == "win32":
        # npx is a .cmd shim on Windows; it cannot be spawned without the shell.
        return {"type": "stdio", "command": "cmd", "args": ["/c", "npx", *args]}
    return {"type": "stdio", "command": "npx", "args": args}


def install_credentials(ctx: PolicyContext, credentials: dict[str, tuple[str, ...]]) -> None:
    ctx.extra[_CREDENTIALS_KEY] = dict(credentials)


def install_interact_domains(ctx: PolicyContext, domains: tuple[str, ...]) -> None:
    ctx.extra[_INTERACT_KEY] = tuple(domains)


def _credential_refs(action: str, tool_input: dict[str, Any], names: set[str]) -> set[str]:
    """Secret names the call asks Playwright to type (it substitutes exact matches only)."""
    if action == "browser_type":
        values = [tool_input.get("text")]
    elif action == "browser_fill_form":
        values = [f.get("value") for f in tool_input.get("fields") or [] if isinstance(f, dict)]
    else:
        return set()
    return {str(v) for v in values if isinstance(v, str) and v in names}


def _classify_credentials(action: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision | None:
    credentials: dict[str, tuple[str, ...]] = ctx.extra.get(_CREDENTIALS_KEY) or {}
    refs = _credential_refs(action, tool_input, set(credentials))
    if not refs:
        return None
    host = str(ctx.extra.get(_HOST_KEY, ""))
    if not host:
        return Decision(Tier.BLOCK, "No sé en qué web está el navegador; no escribo credenciales a ciegas.")
    for name in sorted(refs):
        if not any(host == d or host.endswith("." + d) for d in credentials[name]):
            return Decision(
                Tier.BLOCK, f"La credencial {name} solo se puede usar en {', '.join(credentials[name])}, no en {host}."
            )
    names = ", ".join(sorted(refs))
    other_fields = [
        f for f in tool_input.get("fields") or [] if isinstance(f, dict) and f.get("value") not in credentials
    ]
    if action == "browser_fill_form" and other_fields:
        return Decision(Tier.APPROVAL, f"Rellenar un formulario en {host} con {names} y otros datos.")
    if ctx.tainted:
        return Decision(Tier.APPROVAL, f"Usar {names} en {host} después de leer contenido externo.")
    return Decision(Tier.NOTIFY, f"He usado {names} para iniciar sesión en {host}.")


def classify(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> Decision | None:
    action = tool_name.split("__")[-1]
    if action in _NEVER:
        return None
    credential_decision = _classify_credentials(action, tool_input, ctx)
    if credential_decision is not None:
        return credential_decision
    if action == "browser_navigate":
        return classify_url(str(tool_input.get("url", "")), ctx)
    host = str(ctx.extra.get(_HOST_KEY, ""))
    if action in _HAND_OVER or action == "browser_fill_form":
        return Decision(Tier.APPROVAL, f"Enviar datos o archivos en {host or 'una web'}.")
    if action in _INTERACT:
        if host and domain_matches(host, ctx.extra.get(_INTERACT_KEY, ())) and not ctx.tainted:
            return Decision(Tier.NOTIFY, f"Interactuar con {host} (web autorizada para clics).")
        return Decision(Tier.APPROVAL, f"Interactuar con {host or 'una web'}: hacer clic o escribir.")
    return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def observe(tool_name: str, tool_input: dict[str, Any], ctx: PolicyContext) -> None:
    if not tool_name.startswith(f"mcp__{SERVER_NAME}__"):
        return
    action = tool_name.split("__")[-1]
    if action == "browser_navigate":
        ctx.extra[_HOST_KEY] = _host(str(tool_input.get("url", "")))
    elif action in _MAY_NAVIGATE:
        ctx.extra[_HOST_KEY] = ""


def observe_result(tool_name: str, tool_response: Any, ctx: PolicyContext) -> None:
    """Playwright reports the page it is on after each action; trust that over our own guess."""
    if not tool_name.startswith(f"mcp__{SERVER_NAME}__") or tool_response is None:
        return
    text = tool_response if isinstance(tool_response, str) else json.dumps(tool_response, default=str)
    urls = _PAGE_URL_RE.findall(text)
    if urls:
        ctx.extra[_HOST_KEY] = _host(urls[-1].rstrip('",\\'))


def dotenv_line(name: str, value: str) -> str:
    """One dotenv entry Playwright parses back verbatim, whatever the password contains."""
    if "\n" in value or "\r" in value:
        raise ValueError(f"La credencial {name} tiene saltos de línea; no se puede usar en el navegador.")
    for quote in ("'", '"', "`"):
        if quote not in value:
            return f"{name}={quote}{value}{quote}"
    raise ValueError(f"La credencial {name} contiene a la vez comillas simples, dobles e invertidas.")


def write_secrets_file(path: Path, values: dict[str, str]) -> None:
    """Plain-text file only the browser reads; it lives in Jarvis's protected data dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(dotenv_line(n, v) + "\n" for n, v in sorted(values.items())), encoding="utf-8")
    restrict_to_owner(path)


def restrict_to_owner(path: Path) -> None:
    if sys.platform == "win32":
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"], check=False, capture_output=True
            )
    else:
        path.chmod(0o600)


def find_browser(channel: str) -> str | None:
    names = {"msedge": ("msedge", "microsoft-edge"), "chrome": ("chrome", "google-chrome")}.get(channel, ())
    for name in names:
        if found := shutil.which(name):
            return found
    if sys.platform == "win32":
        folder = {"msedge": r"Microsoft\Edge\Application\msedge.exe", "chrome": r"Google\Chrome\Application\chrome.exe"}
        for base in (
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if base and channel in folder and (candidate := Path(base) / folder[channel]).is_file():
                return str(candidate)
    return None


def open_login_window(profile_dir: Path, channel: str, url: str) -> subprocess.Popen[bytes]:
    """Open the browser with Jarvis's profile so the owner can sign in by hand. Jarvis must be stopped."""
    executable = find_browser(channel)
    if executable is None:
        raise FileNotFoundError(f"No encuentro el navegador '{channel}'. Instálalo o cambia browser.channel.")
    profile_dir.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([executable, f"--user-data-dir={profile_dir}", "--no-first-run", url])
