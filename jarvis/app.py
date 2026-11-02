"""Wires everything together and runs Telegram polling and the web chat in one event loop."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from callgate import (
    ApprovalBroker,
    Notifier,
    Observer,
    PermissionGate,
    PolicyContext,
    RedactingFilter,
    Redactor,
    ResultObserver,
    ToolRegistry,
)

from jarvis.brain import Brain
from jarvis.budget import BudgetGuard
from jarvis.config import Settings
from jarvis.core.events import EventBus
from jarvis.core.service import BusNotifier, JarvisCore
from jarvis.memory import MemoryFile
from jarvis.secrets import SecretStore
from jarvis.security import SECRET_MASK, StoreAuditSink, StoreKillSwitch, build_inspector, spanish_messages
from jarvis.small_model import build_small_model
from jarvis.store import Store
from jarvis.telegram.bot import TelegramBot
from jarvis.web.app import WebChannel

_log = logging.getLogger(__name__)


class StartupError(RuntimeError):
    pass


def setup_logging(settings: Settings, redactor: Redactor, verbose: bool = False) -> None:
    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    redacting = RedactingFilter(redactor)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / "jarvis.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        ),
    ]
    if sys.stderr is not None:  # None when started hidden by the Windows autostart
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.addFilter(redacting)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # httpx logs full request URLs at INFO and the Telegram token is part of the URL.
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def ensure_dirs(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for directory in settings.allowed_dirs:
        directory.mkdir(parents=True, exist_ok=True)


@dataclass
class Jarvis:
    settings: Settings
    store: Store
    redactor: Redactor
    core: JarvisCore
    budget: BudgetGuard
    brain: Brain
    bot: TelegramBot | None
    web: WebChannel
    scheduler: Any = None
    watcher: Any = None
    extensions: list[Any] = field(default_factory=list)
    tasks: list[Any] = field(default_factory=list)


@dataclass
class Integrations:
    mcp_servers: dict[str, Any]
    registry: ToolRegistry
    observers: tuple[Observer, ...]
    result_observers: tuple[ResultObserver, ...]
    enabled: tuple[str, ...]
    google_services: Callable[[], tuple[Any, Any]] | None = None


def _prepare_browser_credentials(settings: Settings, secrets: SecretStore) -> Path | None:
    from jarvis.integrations.browser import write_secrets_file

    target = settings.browser_secrets_file
    values: dict[str, str] = {}
    for name in settings.browser_credentials:
        value = secrets.get(name)
        if value:
            values[name] = value
        else:
            _log.warning("Credencial %s sin guardar: python -m jarvis secrets set %s", name, name)
    if not values:
        target.unlink(missing_ok=True)
        return None
    write_secrets_file(target, values)
    return target


def build_integrations(settings: Settings, secrets: SecretStore, store: Store | None = None) -> Integrations:
    from jarvis import memory, vault
    from jarvis.integrations import browser, github, google_auth, google_tools
    from jarvis.security import core_registry

    servers: dict[str, Any] = {}
    registry = core_registry()
    observers: list[Observer] = []
    result_observers: list[ResultObserver] = []
    enabled: list[str] = []

    if settings.browser_enabled and settings.browser_mode == "remote":
        servers[browser.SERVER_NAME] = browser.remote_config(settings.browser_url)
        observers.append(browser.observe)
        result_observers.append(browser.observe_result)
        enabled.append("navegador (contenedor)")
    elif settings.browser_enabled:
        settings.browser_profile_dir.mkdir(parents=True, exist_ok=True)
        settings.browser_output_dir.mkdir(parents=True, exist_ok=True)
        secrets_file = _prepare_browser_credentials(settings, secrets)
        servers[browser.SERVER_NAME] = browser.mcp_config(
            settings.browser_profile_dir,
            settings.browser_output_dir,
            settings.browser_channel,
            settings.browser_headless,
            secrets_file,
        )
        observers.append(browser.observe)
        result_observers.append(browser.observe_result)
        enabled.append("navegador")

    if settings.github_enabled:
        token = secrets.get("GITHUB_TOKEN")
        if token:
            servers[github.SERVER_NAME] = github.mcp_config(
                token, settings.github_toolsets, settings.github_mode, settings.github_local_command
            )
            enabled.append("GitHub")
        else:
            _log.warning("GitHub activado pero falta GITHUB_TOKEN: python -m jarvis secrets set GITHUB_TOKEN")

    google_services: Callable[[], tuple[Any, Any]] | None = None
    if settings.google_enabled and google_auth.is_connected(secrets):
        cache: dict[str, tuple[Any, Any]] = {}

        def services() -> tuple[Any, Any]:
            if "s" not in cache:
                cache["s"] = google_auth.build_services(secrets)
            return cache["s"]

        google_services = services
        servers[google_tools.SERVER_NAME] = google_tools.build_server(services, registry)
        enabled.append("Gmail y Calendar")

    if store is not None and settings.memory_file is not None:
        memory_file = memory.MemoryFile(settings.memory_file)
        memory_file.ensure()
        servers[memory.SERVER_NAME] = memory.build_server(store, memory_file, registry)
        enabled.append("memoria")

    if settings.vault_dir is not None:
        jarvis_vault = vault.Vault(settings.vault_dir)
        jarvis_vault.ensure()
        servers[vault.SERVER_NAME] = vault.build_server(jarvis_vault, registry)
        enabled.append("vault de Obsidian")

    return Integrations(servers, registry, tuple(observers), tuple(result_observers), tuple(enabled), google_services)


def load_playbooks_into(
    settings: Settings,
    secrets: SecretStore,
    store: Store,
    brain: Brain,
    notifier: Notifier,
    integrations: Integrations,
    guards: list[Any],
    summarizers: dict[str, Any],
) -> list[Any]:
    """Load the playbooks enabled in jarvis.yaml and register their tools, tiers, guards and hooks."""
    from jarvis.playbooks import PlaybookContext, build_tools, instructions_section, load_playbook

    loaded = []
    mail_hooks: list[Any] = []
    for name, override in settings.playbooks.items():
        if not override.get("enabled", True):
            continue
        playbook = load_playbook(settings.playbooks_dir, name, override)
        allowed = set(playbook.allowed_secrets)

        def get_secret(secret_name: str, _allowed: set[str] = allowed) -> str | None:
            if secret_name not in _allowed:
                raise PermissionError(f"Este playbook no tiene acceso a {secret_name}.")
            return secrets.get(secret_name)

        ctx = PlaybookContext(
            name=name,
            settings=playbook.settings,
            limits=playbook.limits,
            db_path=settings.db_path,
            data_dir=settings.data_dir / "playbooks" / name,
            work_dir=settings.workspace / name,
            notify=notifier.notify,
            complete=brain.complete,
            run_subagent=brain.run_subagent,
            get_secret=get_secret,
            google_services=integrations.google_services,
        )
        ctx.data_dir.mkdir(parents=True, exist_ok=True)
        built = build_tools(playbook, ctx)
        from claude_agent_sdk import create_sdk_mcp_server

        brain.add_mcp_server(playbook.server_name, create_sdk_mcp_server(playbook.server_name, tools=built.tools))
        integrations.registry.add_server(playbook.server_name, built.specs)
        for tool_name, summarizer in built.summarizers.items():
            summarizers[f"mcp__{playbook.server_name}__{tool_name}"] = summarizer
        if built.guard is not None:
            guards.append(built.guard)
        if built.on_email is not None:
            mail_hooks.append(built.on_email)
        loaded.append(playbook)
        _log.info("Playbook activo: %s", playbook.title)
    brain.extra_prompt = instructions_section(loaded)
    return mail_hooks


def build(settings: Settings, secrets: SecretStore) -> Jarvis:
    ensure_dirs(settings)
    store = Store(settings.db_path)
    store.init()
    redactor = Redactor(secrets.values(), mask=SECRET_MASK)

    api_key = secrets.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise StartupError("Falta la clave de Anthropic. Guárdala con:  python -m jarvis secrets set ANTHROPIC_API_KEY")

    broker = ApprovalBroker(settings.approval_timeout_s)
    bus = EventBus()
    notifier = BusNotifier(bus)

    budget = BudgetGuard(store, notifier, settings.daily_budget_usd, settings.task_budget_usd)
    integrations = build_integrations(settings, secrets, store)
    _log.info("Integraciones activas: %s", ", ".join(integrations.enabled) or "ninguna")

    guards: list[Any] = []
    summarizers: dict[str, Any] = {}

    messages = spanish_messages(settings.owner_name)
    audit_sink = StoreAuditSink(store)
    kill_switch = StoreKillSwitch(store)
    # The small model needs the brain, which needs the gate factory: it is filled in right after.
    inspection: dict[str, Any] = {"inspector": None}

    def gate_factory(conversation_id: str) -> PermissionGate:
        from jarvis.integrations.browser import install_credentials, install_interact_domains
        from jarvis.integrations.github import install_repo_allowlist

        context = PolicyContext(
            registry=integrations.registry,
            allowed_dirs=settings.allowed_dirs,
            protected_paths=settings.protected_paths,
            trusted_domains=settings.trusted_domains,
            rules=settings.tool_rules,
            cwd=settings.workspace,
            observers=integrations.observers,
            result_observers=integrations.result_observers,
            summarizers=summarizers,
            guards=tuple(guards),
            messages=messages,
        )
        install_repo_allowlist(context, settings.github_repos)
        install_credentials(context, settings.browser_credentials)
        install_interact_domains(context, settings.browser_interact_domains)
        return PermissionGate(
            context=context,
            redactor=redactor,
            broker=broker,
            notifier=notifier,
            audit=audit_sink,
            conversation_id=conversation_id,
            kill_switch=kill_switch,
            inspector=inspection["inspector"],
        )

    brain = Brain(
        settings=settings,
        store=store,
        api_key=api_key,
        redactor=redactor,
        budget=budget,
        gate_factory=gate_factory,
        mcp_servers=integrations.mcp_servers,
        memory_reader=MemoryFile(settings.memory_file).read if settings.memory_file else None,
    )
    small_model = build_small_model(settings, brain)
    inspection["inspector"] = build_inspector(settings.injection_check, small_model)

    mail_hooks = load_playbooks_into(settings, secrets, store, brain, notifier, integrations, guards, summarizers)

    from jarvis.scheduler import Scheduler, load_tasks

    core = JarvisCore(
        store=store,
        brain=brain,
        broker=broker,
        kill_switch=kill_switch,
        bus=bus,
        notifier=notifier,
        budget=budget,
        max_parallel=settings.max_parallel_tasks,
    )
    tasks = load_tasks(settings.tasks_file) if settings.tasks_file else []
    scheduler = Scheduler(core.submit_scheduled, tasks)
    core.scheduler = scheduler
    from jarvis.extensions import ExtensionContext, load_all

    extensions = load_all(settings.extensions)
    for extension in extensions:
        extension.declare(integrations.registry)
        extension.setup(
            ExtensionContext(
                settings=settings,
                config=settings.section(extension.name),
                store=store,
                brain=brain,
                core=core,
                notifier=notifier,
                integrations=integrations,
                scheduler=scheduler,
            )
        )
        _log.info("Extensión activa: %s", extension.name)

    # Channels: each one only talks to the core. Adding one is a line here, never a change to the core.
    bot: TelegramBot | None = None
    telegram_token = secrets.get("TELEGRAM_BOT_TOKEN")
    if telegram_token:
        from jarvis.telegram.client import TelegramClient

        voice = None
        if settings.voice is not None:
            from jarvis.voice import VoiceboxClient

            voice = VoiceboxClient(settings.voice)
            _log.info("Voz activa: Voicebox en %s (perfil %s)", settings.voice.url, settings.voice.profile)
        bot = TelegramBot(TelegramClient(telegram_token), core, settings.telegram_chat_id, voice)
        core.register(bot)
    web = WebChannel()
    core.register(web)

    watcher = None
    if settings.watcher_enabled and integrations.google_services is not None and bot is not None:
        from jarvis.watcher import Watcher

        watcher = Watcher(
            store=store,
            notifier=notifier,
            services=integrations.google_services,
            completer=small_model if settings.watcher_triage else None,
            interval_s=settings.watcher_interval_s,
            meeting_lead_min=settings.meeting_alert_min,
            mail_hooks=tuple(mail_hooks),
            is_paused=kill_switch.engaged,
            inspector=inspection["inspector"],
            describe_reason=messages.reason,
            owner=settings.owner_name,
            owner_about=settings.owner_about,
        )
    return Jarvis(settings, store, redactor, core, budget, brain, bot, web, scheduler, watcher, extensions, tasks)


def web_url(settings: Settings) -> str:
    from jarvis.web.app import load_or_create_token

    token = load_or_create_token(settings.data_dir)
    return f"http://127.0.0.1:{settings.web_port}/?t={token}"


async def serve(jarvis: Jarvis) -> None:
    import uvicorn

    from jarvis.web.app import create_web_app, load_or_create_token

    settings = jarvis.settings
    token = load_or_create_token(settings.data_dir)
    app = create_web_app(core=jarvis.core, token=token, port=settings.web_port, channel=jarvis.web)
    server = uvicorn.Server(
        uvicorn.Config(app, host=settings.web_bind, port=settings.web_port, log_config=None, access_log=False)
    )

    tasks = [
        asyncio.create_task(server.serve(), name="web"),
        asyncio.create_task(jarvis.core.queue.run_forever(), name="queue"),
    ]
    if jarvis.bot is not None:
        tasks.append(asyncio.create_task(jarvis.bot.run_forever(), name="telegram"))
    else:
        _log.warning("Sin TELEGRAM_BOT_TOKEN: no habrá avisos ni aprobaciones (todo lo que las necesite se denegará).")
    if jarvis.watcher is not None:
        tasks.append(asyncio.create_task(jarvis.watcher.run_forever(), name="watcher"))
    if jarvis.scheduler is not None:
        jarvis.scheduler.start()

    print(f"\nJarvis en marcha. Chat web: http://127.0.0.1:{settings.web_port}/?t={token}\n", flush=True)
    if jarvis.bot is not None:
        await jarvis.bot.say("🟢 Jarvis está en marcha.", with_stop_button=True)
    await jarvis.core.startup(jarvis.tasks)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        if jarvis.scheduler is not None:
            jarvis.scheduler.shutdown()
        for task in tasks:
            task.cancel()
        jarvis.settings.browser_secrets_file.unlink(missing_ok=True)
        if jarvis.bot is not None:
            await jarvis.bot.client.close()
            if jarvis.bot.voice is not None:
                await jarvis.bot.voice.close()
