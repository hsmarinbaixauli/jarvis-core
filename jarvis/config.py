"""Non-secret settings, loaded from ``jarvis.yaml``. Secrets live in the OS keyring (see secrets.py)."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from callgate import Tier, ToolRule

from jarvis.voice import VoiceSettings

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
PACKAGE_DIR: Path = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH: Path = Path(os.environ.get("JARVIS_CONFIG") or PROJECT_ROOT / "jarvis.yaml")
# Set in the Docker image. The web server then listens on all interfaces *inside* the container, and
# compose publishes it only on the host's 127.0.0.1.
IN_CONTAINER = os.environ.get("JARVIS_IN_CONTAINER") == "1"
DEFAULT_OWNER = "tu usuario"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    config_path: Path
    data_dir: Path
    allowed_dirs: tuple[Path, ...]
    trusted_domains: tuple[str, ...] = ()
    model: str = "claude-sonnet-5-5"
    fast_model: str = "claude-haiku-5-5"
    # Model for small jobs (triage, injection checks); None → fast_model. See jarvis/small_model.py.
    small_model: str | None = None
    # How external content is checked for instructions aimed at Jarvis: confirm | heuristic | off.
    injection_check: str = "confirm"
    max_turns: int = 30
    task_budget_usd: float = 1.0
    daily_budget_usd: float = 10.0
    approval_timeout_s: int = 600
    telegram_chat_id: int | None = None
    web_host: str = "127.0.0.1"
    web_bind: str = "127.0.0.1"
    web_port: int = 8765
    tool_rules: tuple[ToolRule, ...] = field(default_factory=tuple)
    # Background tasks running at once (each one is a model session); more wait in the queue.
    max_parallel_tasks: int = 2
    browser_enabled: bool = True
    browser_channel: str = "chrome"
    # local: Jarvis starts Playwright MCP itself (npx). remote: it talks to the browser container over HTTP.
    browser_mode: str = "local"
    browser_url: str = ""
    browser_headless: bool = True
    # Keyring secret name → domains where the browser may type it (see integrations/browser.py).
    browser_credentials: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Sites where clicks and typing only notify. Elsewhere every interaction waits for approval.
    browser_interact_domains: tuple[str, ...] = ()
    github_enabled: bool = False
    github_mode: str = "remote"
    github_repos: tuple[str, ...] = ()
    github_toolsets: tuple[str, ...] = ("repos", "issues", "pull_requests", "actions")
    github_local_command: str = "github-mcp-server"
    google_enabled: bool = True
    tasks_file: Path | None = None
    memory_file: Path | None = None
    # Jarvis's own Obsidian vault (a folder of Markdown notes); None → no vault tools. See jarvis/vault.py.
    vault_dir: Path | None = None
    # Voice notes in Telegram through a local Voicebox; None → off. See jarvis/voice.py.
    voice: VoiceSettings | None = None
    watcher_enabled: bool = True
    watcher_interval_s: int = 300
    meeting_alert_min: int = 15
    watcher_triage: bool = True
    playbooks: dict[str, dict[str, Any]] = field(default_factory=dict)
    playbooks_path: Path | None = None
    # Who Jarvis works for: used in prompts and messages. Personal data lives here, never in the code.
    owner_name: str = DEFAULT_OWNER
    owner_about: str = ""
    # Python modules plugged in at startup (see jarvis/extensions.py); each reads its own section below.
    extensions: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def playbooks_dir(self) -> Path:
        return self.playbooks_path or PROJECT_ROOT / "playbooks"

    def section(self, name: str) -> dict[str, Any]:
        value = self.raw.get(name) or {}
        if not isinstance(value, dict):
            raise ConfigError(f"'{name}' debe ser un diccionario.")
        return value

    @property
    def browser_profile_dir(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def browser_secrets_file(self) -> Path:
        return self.data_dir / "browser-secrets.env"

    @property
    def browser_output_dir(self) -> Path:
        return self.workspace / "navegador"

    @property
    def workspace(self) -> Path:
        return self.allowed_dirs[0]

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jarvis.db"

    @property
    def protected_paths(self) -> tuple[Path, ...]:
        """Paths the agent may never read or write, even if an allowed dir contains them."""
        extra = tuple(
            p for p in (self.tasks_file, self.memory_file, self.playbooks_dir, self.vault_dir) if p is not None
        )
        return (PACKAGE_DIR, self.config_path, self.data_dir, *extra)


def _resolve(raw: str, base: Path) -> Path:
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


def number(data: dict[str, Any], key: str, default: float, minimum: float) -> float:
    """A numeric setting with a lower bound. Public: extensions validate their own sections with it."""
    return _number(data, key, default, minimum)


def flag(data: dict[str, Any], key: str, default: bool) -> bool:
    return _flag(data, key, default)


def _number(data: dict[str, Any], key: str, default: float, minimum: float) -> float:
    value = data.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool) or value < minimum:
        raise ConfigError(f"'{key}' debe ser un número mayor o igual que {minimum}.")
    return float(value)


def _small_model(raw: Any) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'small_model' debe ser un diccionario: {provider: claude, model: ...}.")
    provider = str(raw.get("provider", "claude"))
    if provider != "claude":
        raise ConfigError(
            f"small_model.provider '{provider}' todavía no está disponible: de momento solo 'claude'. "
            "El modelo local (Ollama) está previsto; ver jarvis/small_model.py."
        )
    model = raw.get("model")
    return str(model) if model else None


def _injection_check(raw: Any) -> str:
    value = str(raw)
    if value not in ("confirm", "heuristic", "off"):
        raise ConfigError("'injection_check' debe ser confirm, heuristic u off.")
    return value


def _max_parallel(queue: Any) -> int:
    value = queue.get("max_parallel", 2) if isinstance(queue, dict) else 2
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 8:
        raise ConfigError("'queue.max_parallel' debe ser un número entre 1 y 8.")
    return value


def _parse_rules(raw: Any) -> tuple[ToolRule, ...]:
    if raw in (None, []):
        return ()
    if not isinstance(raw, list):
        raise ConfigError("'tool_rules' debe ser una lista de {pattern, tier}.")
    rules: list[ToolRule] = []
    for item in raw:
        if not isinstance(item, dict) or "pattern" not in item or "tier" not in item:
            raise ConfigError("Cada regla de 'tool_rules' necesita 'pattern' y 'tier'.")
        try:
            tier = Tier(str(item["tier"]).lower())
        except ValueError as exc:
            valid = ", ".join(t.value for t in Tier)
            raise ConfigError(f"Nivel desconocido '{item['tier']}'. Usa uno de: {valid}.") from exc
        rules.append(ToolRule(pattern=str(item["pattern"]), tier=tier))
    return tuple(rules)


def _flag(data: dict[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' debe ser true o false.")
    return value


def _str_list(data: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{key}' debe ser una lista de textos.")
    return tuple(v.strip() for v in value if v.strip())


def _parse_credentials(raw: Any) -> dict[str, tuple[str, ...]]:
    from jarvis.secrets import SecretError, validate_name

    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'browser.credentials' debe ser un diccionario: NOMBRE_DE_LA_CLAVE → [dominios].")
    parsed: dict[str, tuple[str, ...]] = {}
    for name, domains in raw.items():
        try:
            validate_name(str(name))
        except SecretError as exc:
            raise ConfigError(f"browser.credentials: {exc}") from exc
        if not isinstance(domains, list) or not domains or not all(isinstance(d, str) and d.strip() for d in domains):
            raise ConfigError(f"browser.credentials.{name}: pon la lista de dominios donde se puede usar.")
        cleaned = tuple(d.strip().lower().lstrip(".") for d in domains)
        if any("/" in d or "*" in d or "." not in d for d in cleaned):
            raise ConfigError(f"browser.credentials.{name}: usa dominios como accounts.google.com, sin rutas ni *.")
        parsed[str(name)] = cleaned
    return parsed


_VOICE_HOSTS = ("127.0.0.1", "localhost", "::1", "host.docker.internal")


def _parse_voice(raw: Any) -> VoiceSettings | None:
    if raw in (None, {}):
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'voice' debe ser un diccionario: {enabled: true, url: ..., profile: ...}.")
    if not _flag(raw, "enabled", True):
        return None
    url = str(raw.get("url", "http://127.0.0.1:17493")).strip()
    match = re.match(r"^http://(\[[^\]]+\]|[^/:]+)(:\d+)?/?$", url)
    if not match or match.group(1).strip("[]").lower() not in _VOICE_HOSTS:
        raise ConfigError(
            "'voice.url' debe ser el Voicebox de este PC (http://127.0.0.1:17493, o "
            "http://host.docker.internal:17493 desde Docker): el audio no sale del ordenador."
        )
    profile = str(raw.get("profile") or "").strip()
    if not profile:
        raise ConfigError("'voice.profile' debe ser el nombre del perfil de voz de Voicebox que usará Jarvis.")
    language = str(raw.get("language", "es"))
    if not re.fullmatch(r"[a-z]{2}", language):
        raise ConfigError("'voice.language' debe ser un código de idioma de dos letras (es, en…).")
    whisper = str(raw.get("whisper_model", "turbo"))
    if whisper not in ("base", "small", "medium", "large", "turbo"):
        raise ConfigError("'voice.whisper_model' debe ser base, small, medium, large o turbo.")
    return VoiceSettings(
        url=url,
        profile=profile,
        language=language,
        whisper_model=whisper,
        engine=str(raw["engine"]) if raw.get("engine") else None,
        model_size=str(raw["model_size"]) if raw.get("model_size") else None,
        reply_with_voice=_flag(raw, "reply_with_voice", True),
        max_seconds=int(_number(raw, "max_seconds", 300, 5)),
        max_reply_chars=int(_number(raw, "max_reply_chars", 1500, 50)),
    )


def _parse_playbooks(raw: Any) -> dict[str, dict[str, Any]]:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("'playbooks' debe ser un diccionario: nombre → {enabled, settings, limits}.")
    parsed: dict[str, dict[str, Any]] = {}
    for name, value in raw.items():
        value = value or {}
        if not isinstance(value, dict):
            raise ConfigError(f"La configuración del playbook '{name}' debe ser un diccionario.")
        for key in ("settings", "limits"):
            if value.get(key) is not None and not isinstance(value[key], dict):
                raise ConfigError(f"'playbooks.{name}.{key}' debe ser un diccionario.")
        parsed[str(name)] = {
            "enabled": _flag(value, "enabled", True),
            "settings": value.get("settings") or {},
            "limits": value.get("limits") or {},
        }
    return parsed


def load_settings(path: Path | None = None) -> Settings:
    config_path = (path or DEFAULT_CONFIG_PATH).resolve()
    data: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, dict):
            raise ConfigError(f"{config_path.name} debe ser un diccionario YAML.")
        data = loaded or {}
    base = config_path.parent

    data_dir = _resolve(str(data.get("data_dir", "data")), base)
    raw_dirs = data.get("allowed_dirs") or ["~/JarvisTrabajo"]
    if not isinstance(raw_dirs, list):
        raise ConfigError("'allowed_dirs' debe ser una lista de carpetas.")
    allowed_dirs = tuple(_resolve(str(d), base) for d in raw_dirs)

    domains = data.get("trusted_domains") or []
    if not isinstance(domains, list):
        raise ConfigError("'trusted_domains' debe ser una lista de dominios.")

    budget = data.get("budget") or {}
    telegram = data.get("telegram") or {}
    web = data.get("web") or {}
    browser = data.get("browser") or {}
    watcher = data.get("watcher") or {}
    owner = data.get("owner") or {}
    if not isinstance(owner, dict):
        raise ConfigError("'owner' debe ser un diccionario: {name: ..., about: ...}.")
    github = data.get("github") or {}
    google = data.get("google") or {}
    vault = data.get("vault") or {}
    if not isinstance(vault, dict):
        raise ConfigError("'vault' debe ser un diccionario: {enabled: true, path: ...}.")
    vault_path = vault.get("path")
    if _flag(vault, "enabled", bool(vault_path)) and not vault_path:
        raise ConfigError("Con el vault activado, pon en 'vault.path' la carpeta del vault de Jarvis.")

    channel = str(browser.get("channel", "chrome"))
    if channel not in ("msedge", "chrome", "chromium"):
        raise ConfigError("'browser.channel' debe ser chrome, msedge o chromium.")
    browser_mode = str(browser.get("mode", "local"))
    browser_url = str(browser.get("url", "") or "")
    if browser_mode not in ("local", "remote"):
        raise ConfigError("'browser.mode' debe ser local o remote.")
    if browser_mode == "remote":
        if not re.match(r"^https?://[^/\s]+/", browser_url):
            raise ConfigError("Con browser.mode: remote, pon browser.url (p. ej. http://browser:8931/mcp).")
        if browser.get("credentials"):
            raise ConfigError(
                "browser.credentials todavía no funciona con el navegador en Docker: inicia sesión a mano por noVNC."
            )
    github_enabled = _flag(github, "enabled", False)
    github_mode = str(github.get("mode", "remote"))
    if github_mode not in ("remote", "local"):
        raise ConfigError("'github.mode' debe ser remote o local.")
    github_repos = _str_list(github, "repos", ())
    if any(r.count("/") != 1 for r in github_repos):
        raise ConfigError("Cada repo de 'github.repos' debe tener la forma propietario/nombre.")
    if github_enabled and not github_repos:
        raise ConfigError("Con GitHub activado, pon en 'github.repos' los repos con los que Jarvis puede trabajar.")

    chat_id = telegram.get("allowed_chat_id")
    if chat_id is not None and (isinstance(chat_id, bool) or not isinstance(chat_id, int)):
        raise ConfigError("'telegram.allowed_chat_id' debe ser un número (tu chat id).")

    host = str(web.get("host", "127.0.0.1"))
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise ConfigError("El chat web solo puede escuchar en 127.0.0.1 (no se expone a la red).")

    return Settings(
        config_path=config_path,
        data_dir=data_dir,
        allowed_dirs=allowed_dirs,
        trusted_domains=tuple(str(d).lower().lstrip(".") for d in domains),
        model=str(data.get("model", Settings.model)),
        fast_model=str(data.get("fast_model", Settings.fast_model)),
        small_model=_small_model(data.get("small_model")),
        injection_check=_injection_check(data.get("injection_check", "confirm")),
        max_turns=int(_number(data, "max_turns", Settings.max_turns, 1)),
        task_budget_usd=_number(budget, "task_usd", Settings.task_budget_usd, 0.01),
        daily_budget_usd=_number(budget, "daily_usd", Settings.daily_budget_usd, 0.01),
        approval_timeout_s=int(_number(data, "approval_timeout_minutes", 10, 1) * 60),
        telegram_chat_id=chat_id,
        web_host=host,
        web_bind="0.0.0.0" if IN_CONTAINER else host,
        web_port=int(_number(web, "port", Settings.web_port, 1024)),
        tool_rules=_parse_rules(data.get("tool_rules")),
        max_parallel_tasks=_max_parallel(data.get("queue") or {}),
        browser_enabled=_flag(browser, "enabled", True),
        browser_channel=channel,
        browser_mode=browser_mode,
        browser_url=browser_url,
        browser_headless=_flag(browser, "headless", True),
        browser_credentials=_parse_credentials(browser.get("credentials")),
        browser_interact_domains=tuple(d.lower().lstrip(".") for d in _str_list(browser, "interact_domains", ())),
        github_enabled=github_enabled,
        github_mode=github_mode,
        github_repos=github_repos,
        github_toolsets=_str_list(github, "toolsets", Settings.github_toolsets),
        github_local_command=str(github.get("local_command", Settings.github_local_command)),
        google_enabled=_flag(google, "enabled", True),
        tasks_file=_resolve(str(data.get("tasks_file", "tasks.yaml")), base),
        memory_file=_resolve(str(data.get("memory_file", "memory/memoria.md")), base),
        vault_dir=_resolve(str(vault_path), base) if _flag(vault, "enabled", bool(vault_path)) else None,
        voice=_parse_voice(data.get("voice")),
        watcher_enabled=_flag(watcher, "enabled", True),
        watcher_interval_s=int(_number(watcher, "interval_minutes", 5, 1) * 60),
        meeting_alert_min=int(_number(watcher, "meeting_alert_minutes", 15, 1)),
        watcher_triage=_flag(watcher, "triage", True),
        playbooks=_parse_playbooks(data.get("playbooks")),
        playbooks_path=_resolve(str(data["playbooks_dir"]), base) if data.get("playbooks_dir") else None,
        owner_name=str(owner.get("name") or DEFAULT_OWNER).strip(),
        owner_about=str(owner.get("about") or "").strip(),
        extensions=_str_list(data, "extensions", ()),
        raw=data,
    )
