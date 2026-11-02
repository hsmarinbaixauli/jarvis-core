"""Command-line entry point: ``python -m jarvis``."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from jarvis import __version__

if TYPE_CHECKING:
    from jarvis.voice import VoiceSettings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m jarvis", description="Jarvis, asistente personal.")
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="Ruta a jarvis.yaml")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Arrancar Jarvis (Telegram + chat web)")
    run.add_argument("--verbose", action="store_true")

    sub.add_parser("service", help="Arrancar Jarvis vigilado: lo reinicia si se cae (lo usa el arranque automático)")

    secrets = sub.add_parser(
        "secrets", help="Guardar claves (Administrador de credenciales o, en Docker, el almacén del contenedor)"
    )
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    set_cmd = secrets_sub.add_parser("set", help="Guardar o cambiar una clave (se pide sin mostrarla)")
    set_cmd.add_argument("name")
    delete_cmd = secrets_sub.add_parser("delete", help="Borrar una clave")
    delete_cmd.add_argument("name")
    secrets_sub.add_parser("list", help="Ver qué claves hay guardadas (sin mostrar su valor)")
    has_cmd = secrets_sub.add_parser("has", help="Código de salida 0 si la clave está guardada (para scripts)")
    has_cmd.add_argument("name")

    browser = sub.add_parser("browser", help="Navegador de Jarvis")
    browser_sub = browser.add_subparsers(dest="browser_command", required=True)
    login = browser_sub.add_parser("login", help="Abrir el navegador de Jarvis para iniciar sesión a mano")
    login.add_argument("url", nargs="?", default="https://accounts.google.com")

    google = sub.add_parser("google", help="Conectar Gmail y Google Calendar")
    google_sub = google.add_subparsers(dest="google_command", required=True)
    google_login = google_sub.add_parser("login", help="Autorizar a Jarvis en tu cuenta de Google")
    google_login.add_argument("client_secret", type=Path, help="JSON del cliente OAuth de escritorio")
    google_login.add_argument("--port", type=int, default=0, help="Puerto fijo para la vuelta de Google (Docker)")
    google_login.add_argument("--no-browser", action="store_true", help="Imprimir el enlace en vez de abrirlo")

    tasks = sub.add_parser("tasks", help="Tareas programadas (tasks.yaml)")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    tasks_sub.add_parser("list", help="Ver las tareas y validar tasks.yaml")
    run_task = tasks_sub.add_parser("run", help="Ejecutar ahora una tarea (para probarla)")
    run_task.add_argument("task_id")

    sub.add_parser("tools", help="Ver cada herramienta con su efecto (lectura, escritura, irreversible)")
    sub.add_parser("doctor", help="Comprobar la instalación")
    sub.add_parser("web-url", help="Mostrar el enlace del chat web")
    return parser


def _secrets_command(args: argparse.Namespace) -> int:
    from jarvis.secrets import KNOWN_SECRETS, SecretError, SecretStore, validate_name

    store = SecretStore()
    try:
        if args.secrets_command == "set":
            name = validate_name(args.name.upper())
            if name not in KNOWN_SECRETS:
                print(f"Aviso: {name} no es una clave que Jarvis use todavía; la guardo igualmente.")
            value = getpass.getpass(f"Pega el valor de {name} (no se verá) y pulsa Enter: ")
            store.set(name, value)
            from jarvis.config import IN_CONTAINER

            where = "el almacén de claves del contenedor" if IN_CONTAINER else "el Administrador de credenciales"
            print(f"✓ {name} guardada en {where}.")
        elif args.secrets_command == "has":
            return 0 if store.get(validate_name(args.name.upper())) else 1
        elif args.secrets_command == "delete":
            name = validate_name(args.name.upper())
            print(f"✓ {name} borrada." if store.delete(name) else f"{name} no estaba guardada.")
        else:
            saved = set(store.names())
            for name, description in KNOWN_SECRETS.items():
                print(f"{'✓' if name in saved else '·'} {name:<24} {description}")
            for name in sorted(saved - set(KNOWN_SECRETS)):
                print(f"✓ {name}")
    except SecretError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0


def _browser_command(args: argparse.Namespace) -> int:
    from jarvis.config import IN_CONTAINER, load_settings
    from jarvis.integrations.browser import open_login_window

    if IN_CONTAINER:
        print("En Docker se inicia sesión por noVNC: docker compose --profile login up login", file=sys.stderr)
        return 1
    settings = load_settings(args.config)
    print("Cierra Jarvis antes de seguir: el navegador y Jarvis no pueden usar el perfil a la vez.")
    try:
        process = open_login_window(settings.browser_profile_dir, settings.browser_channel, args.url)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("Inicia sesión en las webs que quieras que Jarvis use y cierra la ventana al terminar.")
    process.wait()
    print("✓ Sesiones guardadas en el perfil de Jarvis.")
    return 0


def _google_command(args: argparse.Namespace) -> int:
    from jarvis.integrations.google_auth import login
    from jarvis.secrets import SecretStore

    if not args.client_secret.is_file():
        print(f"Error: no existe {args.client_secret}", file=sys.stderr)
        return 1
    print(
        "Abre el enlace que aparece abajo en tu navegador, con la cuenta de Google de Jarvis, y autoriza."
        if args.no_browser
        else "Se abrirá el navegador para que autorices a Jarvis (Gmail y Calendar)…"
    )
    try:
        login(args.client_secret, SecretStore(), port=args.port, open_browser=not args.no_browser)
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print("✓ Google conectado. Ya puedes borrar el archivo JSON que descargaste.")
    return 0


def _tasks_command(args: argparse.Namespace) -> int:
    from jarvis.config import load_settings
    from jarvis.scheduler import TaskConfigError, load_tasks

    settings = load_settings(args.config)
    try:
        tasks = load_tasks(settings.tasks_file) if settings.tasks_file else []
    except TaskConfigError as exc:
        print(f"Error en {settings.tasks_file}: {exc}", file=sys.stderr)
        return 1
    if args.tasks_command == "list":
        if not tasks:
            print(f"No hay tareas en {settings.tasks_file}. Copia tasks.example.yaml para empezar.")
        for task in tasks:
            state = "" if task.enabled else " (desactivada)"
            print(f"• {task.id}: {task.cron} · avisar: {task.notify}{state}\n    {task.prompt[:100]}")
        return 0

    task = next((t for t in tasks if t.id == args.task_id), None)
    if task is None:
        print(f"No existe la tarea '{args.task_id}'.", file=sys.stderr)
        return 1
    from jarvis.app import build
    from jarvis.core.service import Halted
    from jarvis.secrets import SecretStore

    jarvis = build(settings, SecretStore())

    async def run_once() -> None:
        # Poll Telegram meanwhile, or ✅ taps for this task would never arrive.
        polling = asyncio.create_task(jarvis.bot.run_forever()) if jarvis.bot is not None else None
        try:
            done = await jarvis.core.run_scheduled_now(task)
            print(done["result"] if done["status"] == "done" else f"⚠️ {done['error'] or done['status']}")
        except Halted as exc:
            print(f"Error: {exc}", file=sys.stderr)
        finally:
            if polling is not None:
                polling.cancel()
            if jarvis.bot is not None:
                await jarvis.bot.client.close()

    asyncio.run(run_once())
    return 0


def _service(args: argparse.Namespace) -> int:
    import logging

    from jarvis.config import load_settings
    from jarvis.secrets import SecretStore
    from jarvis.supervisor import supervise, telegram_alert

    settings = load_settings(args.config)
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=log_dir / "supervisor.log",
        level=logging.INFO,
        encoding="utf-8",
        format="%(asctime)s %(levelname)s %(message)s",
    )
    alert = None
    try:
        token = SecretStore().get("TELEGRAM_BOT_TOKEN")
        if token and settings.telegram_chat_id is not None:
            alert = telegram_alert(token, settings.telegram_chat_id)
    except Exception:
        logging.exception("Could not prepare the Telegram alert")
    cmd = [sys.executable, "-m", "jarvis"]
    if args.config is not None:
        cmd += ["--config", str(args.config)]
    try:
        return supervise([*cmd, "run"], on_give_up=alert)
    except KeyboardInterrupt:
        return 0


def _doctor(args: argparse.Namespace) -> int:
    from jarvis.config import ConfigError, load_settings
    from jarvis.secrets import SecretStore

    ok = True
    try:
        settings = load_settings(args.config)
        print(
            f"✓ Configuración: {settings.config_path}"
            + ("" if settings.config_path.is_file() else " (no existe; uso valores por defecto)")
        )
    except ConfigError as exc:
        print(f"✗ Configuración: {exc}")
        return 1
    print(f"✓ Datos en: {settings.data_dir}")
    for directory in settings.allowed_dirs:
        print(f"{'✓' if directory.is_dir() else '·'} Carpeta permitida: {directory}")
    store = SecretStore()
    saved = set(store.names())
    for name, required in (("ANTHROPIC_API_KEY", True), ("TELEGRAM_BOT_TOKEN", False)):
        present = name in saved
        ok &= present or not required
        print(
            f"{'✓' if present else '✗' if required else '·'} {name}"
            + ("" if present else f"  →  python -m jarvis secrets set {name}")
        )
    if settings.telegram_chat_id is None:
        print("· telegram.allowed_chat_id sin configurar (escríbele al bot y te dirá tu id)")
    else:
        print(f"✓ Chat de Telegram permitido: {settings.telegram_chat_id}")
    if settings.browser_enabled and settings.browser_mode == "remote":
        print(f"· Navegador en su contenedor: {settings.browser_url}")
    elif settings.browser_enabled:
        import shutil

        from jarvis.integrations.browser import find_browser

        has_npx = shutil.which("npx") is not None
        ok &= has_npx
        print(
            f"{'✓' if has_npx else '✗'} Node.js (npx) para el navegador" + ("" if has_npx else "  →  instala Node LTS")
        )
        has_browser = find_browser(settings.browser_channel) is not None
        hint = "" if has_browser else "  →  instala Google Chrome"
        print(f"{'✓' if has_browser else '✗'} Navegador {settings.browser_channel}{hint}")
        for name, domains in settings.browser_credentials.items():
            present = name in saved
            ok &= present
            hint = "" if present else f"  →  python -m jarvis secrets set {name}"
            print(f"{'✓' if present else '✗'} Credencial {name} (solo en {', '.join(domains)}){hint}")
    if settings.google_enabled:
        from jarvis.integrations.google_auth import is_connected

        connected = is_connected(store)
        hint = "" if connected else "  →  python -m jarvis google login client_secret.json"
        print(f"{'✓' if connected else '·'} Gmail y Calendar{hint}")
    if settings.github_enabled:
        has_token = "GITHUB_TOKEN" in saved
        ok &= has_token
        hint = "" if has_token else "  →  python -m jarvis secrets set GITHUB_TOKEN"
        print(f"{'✓' if has_token else '✗'} GitHub ({', '.join(settings.github_repos)}){hint}")
    if settings.vault_dir is not None:
        print(f"{'✓' if settings.vault_dir.is_dir() else '·'} Vault de Obsidian: {settings.vault_dir}")
    if settings.voice is not None:
        ok &= _doctor_voice(settings.voice)
    return 0 if ok else 1


def _doctor_voice(voice: VoiceSettings) -> bool:
    import httpx

    try:
        base = voice.url.rstrip("/")
        health = httpx.get(f"{base}/health", timeout=5).json()
        names = [str(p.get("name", "")).lower() for p in httpx.get(f"{base}/profiles", timeout=5).json()]
    except (httpx.HTTPError, ValueError):
        print(f"✗ Voicebox en {voice.url}  →  abre la app Voicebox en el PC")
        return False
    gpu = health.get("gpu_type") or ("GPU" if health.get("gpu_available") else "solo CPU (irá lento)")
    print(f"✓ Voicebox en {voice.url} ({gpu})")
    has_profile = voice.profile.lower() in names
    hint = "" if has_profile else "  →  créalo en Voicebox (pestaña Voices) con ese nombre"
    print(f"{'✓' if has_profile else '✗'} Perfil de voz «{voice.profile}»{hint}")
    return has_profile


def _run(args: argparse.Namespace) -> int:
    from callgate import Redactor

    from jarvis.app import StartupError, build, serve, setup_logging
    from jarvis.config import ConfigError, load_settings
    from jarvis.secrets import SecretStore
    from jarvis.security import SECRET_MASK

    try:
        settings = load_settings(args.config)
        secrets = SecretStore()
        setup_logging(settings, Redactor(secrets.values(), mask=SECRET_MASK), verbose=args.verbose)
        jarvis = build(settings, secrets)
    except (ConfigError, StartupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    try:
        asyncio.run(serve(jarvis))
    except KeyboardInterrupt:
        print("\nJarvis detenido.")
    return 0


def _tolerant_output() -> None:
    # Piped output on Windows uses the ANSI code page, which cannot encode ✓ and would crash the CLI.
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    from keyring.errors import KeyringError

    _tolerant_output()
    try:
        return _dispatch(build_parser().parse_args(argv))
    except KeyringError as exc:
        print(f"Error: no puedo acceder al almacén de credenciales del sistema ({exc}).", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "secrets":
        return _secrets_command(args)
    if args.command == "doctor":
        return _doctor(args)
    if args.command == "browser":
        return _browser_command(args)
    if args.command == "google":
        return _google_command(args)
    if args.command == "tasks":
        return _tasks_command(args)
    if args.command == "tools":
        from jarvis.config import ConfigError, load_settings
        from jarvis.extensions import load_all
        from jarvis.security import core_registry

        registry = core_registry()
        try:
            for extension in load_all(load_settings(args.config).extensions):
                extension.declare(registry)
        except ConfigError:
            pass  # no jarvis.yaml yet: list the core tools only
        print(registry.to_markdown())
        print("\nCualquier herramienta que no esté en esta lista se trata como irreversible (pide tu ✅).")
        return 0
    if args.command == "web-url":
        from jarvis.app import web_url
        from jarvis.config import load_settings

        print(web_url(load_settings(args.config)))
        return 0
    if args.command == "run":
        return _run(args)
    if args.command == "service":
        return _service(args)
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
