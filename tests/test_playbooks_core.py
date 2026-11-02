from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from callgate import Decision, Effect, PolicyContext, Tier, ToolSpec, classify
from callgate.claude_code import registry as claude_code_registry

from jarvis.config import PROJECT_ROOT, ConfigError, load_settings
from jarvis.playbooks import (
    PlaybookError,
    discover,
    instructions_section,
    load_playbook,
    subagent_hook,
)

ROOT = PROJECT_ROOT / "playbooks"


@pytest.fixture()
def pb_root(tmp_path: Path) -> Path:
    """A playbooks folder like a private repo's: the template plus a "demo" playbook made from it."""
    import shutil

    root = tmp_path / "playbooks"
    shutil.copytree(ROOT / "_plantilla", root / "_plantilla")
    shutil.copytree(ROOT / "_plantilla", root / "demo")
    (root / "__init__.py").write_text("", encoding="utf-8")
    manifest = root / "demo" / "playbook.yaml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("secrets: []", "secrets: [DEMO_API_KEY]"), encoding="utf-8"
    )
    return root


def test_discover_skips_template(pb_root: Path) -> None:
    assert discover(pb_root) == ["demo"]


def test_load_with_overrides(pb_root: Path) -> None:
    pb = load_playbook(pb_root, "demo", {"settings": {"ejemplo": "mío"}, "limits": {"daily_budget_usd": 1}})
    assert pb.settings["ejemplo"] == "mío" and pb.limits["daily_budget_usd"] == 1
    assert pb.server_name == "pb_demo" and "DEMO_API_KEY" in pb.allowed_secrets


@pytest.mark.parametrize("name", ["../etc", "Webs", "no-existe", "x"])
def test_bad_names_rejected(name: str, pb_root: Path) -> None:
    with pytest.raises(PlaybookError):
        load_playbook(pb_root, name)


def test_template_is_loadable(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    import shutil

    root = tmp_path / "pbs"
    shutil.copytree(ROOT / "_plantilla", root / "mi_negocio")
    (root / "__init__.py").write_text("", encoding="utf-8")
    pb = load_playbook(root, "mi_negocio")
    assert pb.title == "Mi negocio"


def test_undeclared_playbook_tool_blocks_loading(tmp_path: Path) -> None:
    pytest.importorskip("claude_agent_sdk")
    import shutil

    from jarvis.playbooks import build_tools

    root = tmp_path / "pbs"
    shutil.copytree(ROOT / "_plantilla", root / "sin_declarar")
    (root / "__init__.py").write_text("", encoding="utf-8")
    tools_py = root / "sin_declarar" / "tools.py"
    tools_py.write_text(
        tools_py.read_text(encoding="utf-8").replace('ToolSpec("example_hello"', 'ToolSpec("otra_cosa"'),
        encoding="utf-8",
    )
    pb = load_playbook(root, "sin_declarar")
    with pytest.raises(PlaybookError, match="example_hello"):
        build_tools(pb, None)  # type: ignore[arg-type]


def test_declared_playbook_tools_follow_their_effect() -> None:
    ctx = PolicyContext(
        registry=claude_code_registry().add_server(
            "pb_x", [ToolSpec("a", Effect.READ), ToolSpec("send", Effect.IRREVERSIBLE)]
        )
    )
    assert classify("mcp__pb_x__a", {}, ctx).tier is Tier.FREE
    assert classify("mcp__pb_x__send", {}, ctx).tier is Tier.APPROVAL
    assert classify("mcp__pb_x__b", {}, ctx).tier is Tier.APPROVAL


def test_guards_can_only_tighten() -> None:
    def loosen(tool: str, args: dict[str, Any], ctx: PolicyContext) -> Decision:
        return Decision(Tier.FREE, "todo vale")

    def tighten(tool: str, args: dict[str, Any], ctx: PolicyContext) -> Decision | None:
        return Decision(Tier.BLOCK, "no") if tool == "WebSearch" else None

    ctx = PolicyContext(registry=claude_code_registry(), allowed_dirs=(Path("."),), guards=(loosen, tighten))
    assert classify("Bash", {"command": "ls"}, ctx).tier is Tier.BLOCK
    assert classify("WebSearch", {"query": "x"}, ctx).tier is Tier.BLOCK
    assert classify("WebFetch", {"url": "https://nuevo.example/"}, ctx).tier is Tier.NOTIFY


def test_subagent_is_confined(tmp_path: Path) -> None:
    def decision(tool: str, args: dict[str, Any]) -> str:
        out = asyncio.run(subagent_hook(tmp_path, {"tool_name": tool, "tool_input": args}))
        return out["hookSpecificOutput"]["permissionDecision"]

    assert decision("Write", {"file_path": str(tmp_path / "index.html")}) == "allow"
    assert decision("Write", {"file_path": str(tmp_path.parent / "x.html")}) == "deny"
    assert decision("WebFetch", {"url": "https://example.com"}) == "deny"
    assert decision("Bash", {"command": "ls"}) == "deny"


def test_instructions_section(pb_root: Path) -> None:
    pb = load_playbook(pb_root, "demo")
    text = instructions_section([pb])
    assert "Mi negocio" in text and pb.instructions.strip()[:20] in text
    assert instructions_section([]) == ""


def test_playbooks_dir_is_protected(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "jarvis.yaml")
    assert settings.playbooks_dir in settings.protected_paths


def test_playbook_config_parsing(tmp_path: Path) -> None:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("playbooks:\n  demo:\n    settings: {ejemplo: 1}\n", encoding="utf-8")
    assert load_settings(cfg).playbooks["demo"]["enabled"] is True
    cfg.write_text("playbooks: [demo]\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(cfg)


def test_load_playbooks_into_registers_everything(
    tmp_path: Path, pb_root: Path, memory_keyring: Any, store: Any
) -> None:
    pytest.importorskip("claude_agent_sdk")
    from jarvis.app import Integrations, load_playbooks_into
    from jarvis.secrets import SecretStore

    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text(
        f"allowed_dirs: [w]\nplaybooks_dir: {pb_root.as_posix()}\nplaybooks:\n  demo: {{}}\n", encoding="utf-8"
    )
    settings = load_settings(cfg)
    settings.workspace.mkdir()
    secrets = SecretStore(memory_keyring)
    secrets.set("DEMO_API_KEY", "demo-key-123")
    secrets.set("TELEGRAM_BOT_TOKEN", "123456789:" + "a" * 35)

    class StubBrain:
        def __init__(self) -> None:
            self.servers: dict[str, Any] = {}
            self.extra_prompt = ""

        def add_mcp_server(self, name: str, config: Any) -> None:
            self.servers[name] = config

        async def complete(self, *a: Any, **k: Any) -> str:
            return ""

        async def run_subagent(self, **k: Any) -> dict[str, Any]:
            return {}

    class Notifier:
        async def notify(self, text: str) -> None:
            pass

    brain = StubBrain()
    integrations = Integrations({}, claude_code_registry(), (), (), ())
    guards: list[Any] = []
    summarizers: dict[str, Any] = {}
    hooks = load_playbooks_into(settings, secrets, store, brain, Notifier(), integrations, guards, summarizers)  # type: ignore[arg-type]
    assert "pb_demo" in brain.servers and integrations.registry.has_server("pb_demo")
    assert integrations.registry.resolve("mcp__pb_demo__example_hello") is not None
    assert guards == [] and hooks == [] and summarizers == {}  # the template declares none
    assert "Mi negocio" in brain.extra_prompt
