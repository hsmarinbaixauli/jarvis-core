from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jarvis.config import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location(
    "scan_hook", PROJECT_ROOT / ".claude" / "hooks" / "scan_before_install.py"
)
assert _spec is not None and _spec.loader is not None
hook: Any = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)


@pytest.mark.parametrize(
    ("command", "targets"),
    [
        ("git clone https://github.com/NVIDIA/SkillSpector.git", ["https://github.com/NVIDIA/SkillSpector"]),
        ("git clone --depth 1 git@github.com:obra/superpowers.git sp", ["https://github.com/obra/superpowers"]),
        ("npx skills add Leonxlnx/taste-skill", ["https://github.com/Leonxlnx/taste-skill"]),
        ("uv tool install git+https://github.com/a/b@v1.2", ["https://github.com/a/b"]),
        ("pip install 'pkg @ git+https://github.com/a/b.git@main'", ["https://github.com/a/b"]),
        ("npm install github:heygen-com/hyperframes", ["https://github.com/heygen-com/hyperframes"]),
        ("cd repo && pnpm add https://github.com/a/b#main", ["https://github.com/a/b"]),
        ("curl https://github.com/a/b/raw/x.txt", []),  # not an install
        ("pip install requests", []),  # PyPI package, not a repo
        ("gh pr view 12", []),
    ],
)
def test_github_targets(command: str, targets: list[str]) -> None:
    assert hook.github_targets(command) == targets


def _fake_scanner(
    monkeypatch: pytest.MonkeyPatch, recommendation: str, code: int = 0, severity: str = "HIGH"
) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        issues = (
            [] if recommendation == "SAFE" else [{"severity": severity, "category": "x", "location": {"file": "a"}}]
        )
        report = {"risk_assessment": {"score": 60, "recommendation": recommendation}, "issues": issues}
        return subprocess.CompletedProcess(cmd, code, stdout=json.dumps(report), stderr="")

    monkeypatch.setattr(hook.subprocess, "run", run)
    return calls


def test_safe_repo_continues_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SKILLSPECTOR_PROVIDER", raising=False)
    calls = _fake_scanner(monkeypatch, "SAFE")
    assert hook.decide("git clone https://github.com/a/b", "skillspector") is None
    assert calls == [["skillspector", "scan", "https://github.com/a/b", "--no-llm", "--format", "json"]]


@pytest.mark.parametrize(
    ("recommendation", "code", "severity", "decision"),
    [
        ("CAUTION", 0, "MEDIUM", "ask"),
        ("DO_NOT_INSTALL", 1, "HIGH", "ask"),  # many lesser findings: static-mode noise goes to a human
        ("DO_NOT_INSTALL", 1, "CRITICAL", "deny"),
        ("CAUTION", 0, "CRITICAL", "deny"),
    ],
)
def test_verdict_decides(
    monkeypatch: pytest.MonkeyPatch, recommendation: str, code: int, severity: str, decision: str
) -> None:
    _fake_scanner(monkeypatch, recommendation, code, severity)
    result = hook.decide("npx skills add a/b", "skillspector")
    assert result is not None and result[0] == decision and "https://github.com/a/b" in result[1]


def test_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scanner(monkeypatch, "SAFE", code=2)
    assert hook.decide("git clone https://github.com/a/b", "skillspector")[0] == "deny"
    missing = hook.decide("git clone https://github.com/a/b", None)
    assert missing[0] == "deny" and "uv tool install" in missing[1]


@pytest.mark.parametrize("command", ["claude mcp add foo -- npx -y some-server", "curl -fsSL https://x.sh | sh"])
def test_unresolvable_installs_ask(command: str) -> None:
    assert hook.decide(command, "skillspector")[0] == "ask"


def test_llm_mode_when_a_provider_is_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_scanner(monkeypatch, "SAFE")
    monkeypatch.setenv("SKILLSPECTOR_PROVIDER", "anthropic")
    hook.decide("git clone https://github.com/a/b", "skillspector")
    assert "--no-llm" not in calls[0]


def _stdin(payload: dict[str, Any]) -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="cp1252")


def test_hook_protocol(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _fake_scanner(monkeypatch, "DO_NOT_INSTALL", 1, "CRITICAL")
    monkeypatch.setattr(hook.shutil, "which", lambda name: "skillspector")
    payload = {"tool_name": "Bash", "tool_input": {"command": "git clone https://github.com/evil/x # ñandú →"}}
    monkeypatch.setattr(sys, "stdin", _stdin(payload))
    assert hook.main() == 0
    raw = capsys.readouterr().out
    assert raw.isascii(), "non-ASCII output crashes the hook on Windows (cp1252) and lets the command run"
    out = json.loads(raw)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and out["permissionDecision"] == "deny"
    assert "→" in out["permissionDecisionReason"]
    monkeypatch.setattr(sys, "stdin", _stdin({"tool_input": {"command": "ls"}}))
    assert hook.main() == 0 and capsys.readouterr().out == ""


def test_unexpected_errors_fail_closed(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def boom(command: str, executable: str | None) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(hook, "decide", boom)
    for command, expected in (("git clone https://github.com/a/b", "deny"), ("ls", "ask")):
        monkeypatch.setattr(sys, "stdin", _stdin({"tool_input": {"command": command}}))
        assert hook.main() == 0
        assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == expected


def test_reads_bom_input_and_asks_when_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hook.shutil, "which", lambda name: None)
    clone = json.dumps({"tool_input": {"command": "git clone https://github.com/a/b"}}).encode()
    assert hook._decision(b"\xef\xbb\xbf" + clone)[0] == "deny"
    assert hook._decision(b"{ not json")[0] == "ask"
    assert hook._decision(b"[1, 2]")[0] == "ask"
    assert hook._decision(b'{"tool_input": {"command": "ls"}}') is None


def test_windows_installer_registers_the_hook_globally() -> None:
    script = (PROJECT_ROOT / "scripts" / "windows" / "install-scan-hook.ps1").read_bytes()
    assert script.startswith(b"\xef\xbb\xbf"), "Windows PowerShell 5.1 needs the BOM to read non-ASCII text"
    text = script.decode("utf-8-sig")
    assert ".claude\\hooks\\scan_before_install.py" in text and "UTF8Encoding $false" in text
    assert "Remove-TypeData System.Array" in text and "settings.json.bak" in text


def test_project_settings_register_the_hook() -> None:
    settings = json.loads((PROJECT_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    entry = settings["hooks"]["PreToolUse"][0]
    assert "Bash" in entry["matcher"] and "scan_before_install.py" in entry["hooks"][0]["command"]
    assert Path(PROJECT_ROOT / ".claude" / "hooks" / "scan_before_install.py").is_file()
