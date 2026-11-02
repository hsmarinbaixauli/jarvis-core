from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from jarvis.config import load_settings
from jarvis.extensions import ExtensionError, load
from jarvis.prompts import build_system_prompt
from jarvis.security import spanish_messages


def test_owner_comes_from_config(tmp_path: Path) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("owner: {name: Ana, about: diseñadora en Lisboa}\n", encoding="utf-8")
    settings = load_settings(cfg)
    prompt = build_system_prompt((tmp_path,), "Le gusta el café.", "", settings.owner_name, settings.owner_about)
    assert prompt.startswith("Eres Jarvis, el asistente personal de Ana, diseñadora en Lisboa.")
    assert "Lo que sabes de Ana" in prompt and "Le gusta el café." in prompt
    messages = spanish_messages("Ana")
    assert messages("rejected").startswith("Ana no ha aprobado") and messages.reason("inj_conceal").endswith("Ana")


def test_no_personal_name_in_the_core() -> None:
    from jarvis.config import PACKAGE_DIR

    offenders = [
        str(p.relative_to(PACKAGE_DIR)) for p in PACKAGE_DIR.rglob("*.py") if "Hugo" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_default_owner_reads_naturally(tmp_path: Path) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("{}\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.owner_name == "tu usuario" and settings.extensions == ()
    assert settings.memory_file is not None and settings.memory_file.name == "memoria.md"


def test_extensions_and_playbooks_dir_config(tmp_path: Path) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("extensions: [mi_extension]\nplaybooks_dir: ../jarvis-private/playbooks\n", encoding="utf-8")
    settings = load_settings(cfg)
    assert settings.extensions == ("mi_extension",)
    assert settings.playbooks_dir == (tmp_path / ".." / "jarvis-private" / "playbooks").resolve()
    assert settings.playbooks_dir in settings.protected_paths


def test_loading_extensions(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load("tests.demo_extension").name == "demo"
    with pytest.raises(ExtensionError, match="No encuentro"):
        load("jarvis.no_existe")
    with pytest.raises(ExtensionError, match="create"):
        load("jarvis.prompts")


def test_tools_cli_includes_configured_extensions(tmp_path: Path, capsys: Any) -> None:
    from jarvis.__main__ import main

    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("extensions: [tests.demo_extension]\n", encoding="utf-8")
    assert main(["--config", str(cfg), "tools"]) == 0
    assert "mcp__demo__demo_ping" in capsys.readouterr().out
    cfg.write_text("{}\n", encoding="utf-8")
    assert main(["--config", str(cfg), "tools"]) == 0
    assert "mcp__demo__" not in capsys.readouterr().out


def test_build_loads_extensions(tmp_path: Path, memory_keyring: Any) -> None:
    from tests.test_app import build_jarvis

    jarvis = build_jarvis(tmp_path, memory_keyring, "extensions: [tests.demo_extension]\nowner: {name: Ana}\n")
    assert [e.name for e in jarvis.extensions] == ["demo"]
    assert "/ping" in jarvis.core.help_text()
    assert jarvis.brain._gate_factory("c").context.messages("rejected").startswith("Ana no ha aprobado")
