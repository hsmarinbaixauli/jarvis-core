from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.config import ConfigError, load_settings
from jarvis.secrets import FileSecretBackend, SecretStore

REMOTE = "browser: {mode: remote, url: 'http://browser:8931/mcp', channel: chromium}\n"


def settings_from(tmp_path: Path, text: str) -> Any:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("allowed_dirs: [w]\n" + text, encoding="utf-8")
    return load_settings(cfg)


def test_file_secret_backend(tmp_path: Path) -> None:
    store = SecretStore(FileSecretBackend(tmp_path / "secrets"))
    store.set("ANTHROPIC_API_KEY", " sk-ant-x \n")
    store.set("TELEGRAM_BOT_TOKEN", "123:abc")
    assert store.get("ANTHROPIC_API_KEY") == "sk-ant-x" and store.names() == ["ANTHROPIC_API_KEY", "TELEGRAM_BOT_TOKEN"]
    assert store.delete("TELEGRAM_BOT_TOKEN") and store.get("TELEGRAM_BOT_TOKEN") is None
    if sys.platform != "win32":
        assert (tmp_path / "secrets").stat().st_mode & 0o777 == 0o700
        assert (tmp_path / "secrets" / "jarvis.ANTHROPIC_API_KEY").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "secrets").glob("*.tmp"))
    with pytest.raises(ValueError):
        FileSecretBackend(tmp_path / "s").get_password("jarvis", "../escape")


def test_secrets_dir_env_selects_the_file_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import secrets

    monkeypatch.setenv(secrets.SECRETS_DIR_ENV, str(tmp_path / "s"))
    assert isinstance(secrets._default_backend(), FileSecretBackend)


def test_remote_browser_config(tmp_path: Path) -> None:
    settings = settings_from(tmp_path, REMOTE)
    assert (settings.browser_mode, settings.browser_url, settings.browser_channel) == (
        "remote",
        "http://browser:8931/mcp",
        "chromium",
    )


@pytest.mark.parametrize(
    "bad",
    [
        "browser: {mode: remote}\n",
        "browser: {mode: remote, url: 'file:///etc'}\n",
        "browser: {mode: sideways}\n",
        "browser: {channel: firefox}\n",
        "browser: {mode: remote, url: 'http://browser:8931/mcp', credentials: {X_PASSWORD: [a.es]}}\n",
    ],
)
def test_invalid_browser_config(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ConfigError):
        settings_from(tmp_path, bad)


def test_web_binds_inside_the_container_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.config

    assert settings_from(tmp_path, "").web_bind == "127.0.0.1"
    monkeypatch.setattr(jarvis.config, "IN_CONTAINER", True)
    settings = settings_from(tmp_path, "")
    assert settings.web_bind == "0.0.0.0" and settings.web_host == "127.0.0.1"
    with pytest.raises(ConfigError):
        settings_from(tmp_path, "web: {host: 0.0.0.0}\n")  # the YAML itself can never open it


def test_remote_browser_integration(tmp_path: Path, memory_keyring: Any) -> None:
    pytest.importorskip("claude_agent_sdk")
    from jarvis.app import build_integrations

    settings = settings_from(tmp_path, REMOTE)
    settings.workspace.mkdir()
    integrations = build_integrations(settings, SecretStore(memory_keyring))
    assert integrations.mcp_servers["playwright"] == {"type": "http", "url": "http://browser:8931/mcp"}
    assert not settings.browser_profile_dir.exists()  # the profile lives in the browser container


def test_browser_login_command_points_to_novnc(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    import jarvis.config
    from jarvis.__main__ import main

    monkeypatch.setattr(jarvis.config, "IN_CONTAINER", True)
    assert main(["browser", "login"]) == 1
    assert "noVNC" in capsys.readouterr().err


def test_google_login_without_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis.__main__ import main

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "jarvis.integrations.google_auth.login",
        lambda path, secrets, port=0, open_browser=True: calls.append({"port": port, "open": open_browser}),
    )
    monkeypatch.setattr("jarvis.secrets._default_backend", lambda: FileSecretBackend(tmp_path / "s"))
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    assert main(["google", "login", str(client), "--port", "8090", "--no-browser"]) == 0
    assert calls == [{"port": 8090, "open": False}]


def test_compose_file_is_hardened() -> None:
    import yaml

    from jarvis.config import PROJECT_ROOT

    compose = yaml.safe_load((PROJECT_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for name in ("jarvis", "browser"):
        service = {**compose["x-hardened"], **services[name]} if "<<" not in services[name] else services[name]
        assert service.get("read_only") is True and service.get("cap_drop") == ["ALL"]
    assert services["jarvis"]["ports"] == ["127.0.0.1:8765:8765"]
    assert "ports" not in services["browser"]  # only reachable from Jarvis's network
    assert services["login"]["ports"] == ["127.0.0.1:6080:6080"] and services["login"]["profiles"] == ["login"]
    secrets_mounts = [v for s in services.values() for v in s.get("volumes", []) if v.startswith("jarvis-secrets")]
    assert secrets_mounts == ["jarvis-secrets:/secrets"]  # only the core sees the keys
    assert not any("docker.sock" in v for s in services.values() for v in s.get("volumes", []))
    assert os.path.isfile(PROJECT_ROOT / "docker" / "config" / "jarvis.yaml.example")


def test_example_docker_config_loads(tmp_path: Path) -> None:
    from jarvis.config import PROJECT_ROOT

    cfg = tmp_path / "jarvis.yaml"
    example = (PROJECT_ROOT / "docker" / "config" / "jarvis.yaml.example").read_text(encoding="utf-8")
    cfg.write_text(example, encoding="utf-8")
    settings = load_settings(cfg)

    # Container (Linux) paths. A Windows test runner puts a drive in front ("C:/data"): ignore it.
    def linux(path: Path) -> str:
        return path.as_posix().split(":", 1)[-1]

    assert linux(settings.data_dir) == "/data" and [linux(d) for d in settings.allowed_dirs] == ["/work"]
    assert settings.memory_file is not None and linux(settings.memory_file) == "/memory/memoria.md"
    assert settings.browser_mode == "remote"
