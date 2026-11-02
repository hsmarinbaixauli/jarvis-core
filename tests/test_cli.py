from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import __version__
from jarvis.__main__ import main


def test_main_without_arguments_succeeds() -> None:
    assert main([]) == 0


def test_version_flag_prints_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_module_is_runnable() -> None:
    result = subprocess.run([sys.executable, "-m", "jarvis", "--version"], capture_output=True, text=True)
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_secrets_cli_never_prints_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.conftest import MemoryKeyring

    backend = MemoryKeyring()
    import jarvis.secrets as secrets_module

    monkeypatch.setattr(secrets_module, "_default_backend", lambda: backend)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "sk-ant-very-secret-value-0001")

    assert main(["secrets", "set", "anthropic_api_key"]) == 0
    assert main(["secrets", "list"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-very-secret-value-0001" not in out
    assert "✓ ANTHROPIC_API_KEY" in out
    assert backend.get_password("jarvis", "ANTHROPIC_API_KEY") == "sk-ant-very-secret-value-0001"
    assert main(["secrets", "set", "bad name"]) == 2


def test_missing_keyring_backend_is_a_clean_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from keyring.errors import NoKeyringError

    import jarvis.secrets as secrets_module

    def no_backend() -> None:
        raise NoKeyringError("none")

    monkeypatch.setattr(secrets_module, "_default_backend", no_backend)
    assert main(["secrets", "list"]) == 1
    assert "almacén de credenciales" in capsys.readouterr().err


def test_secrets_has_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.secrets as secrets_module
    from tests.conftest import MemoryKeyring

    backend = MemoryKeyring()
    monkeypatch.setattr(secrets_module, "_default_backend", lambda: backend)
    assert main(["secrets", "has", "TELEGRAM_BOT_TOKEN"]) == 1
    backend.set_password("jarvis", "TELEGRAM_BOT_TOKEN", "123456789:abc")
    assert main(["secrets", "has", "telegram_bot_token"]) == 0


def test_windows_scripts_have_bom_for_windows_powershell() -> None:
    from pathlib import Path

    for script in (Path(__file__).resolve().parents[1] / "scripts" / "windows").glob("*.ps1"):
        assert script.read_bytes().startswith(b"\xef\xbb\xbf"), script.name


def test_tools_lists_every_declaration(capsys: pytest.CaptureFixture[str]) -> None:
    from jarvis.__main__ import main

    assert main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "| `mcp__google__gmail_send_email` | irreversible | approval | no |" in out
    assert "| `mcp__playwright__browser_evaluate` | irreversible | block | yes |" in out
    assert "Bash" not in out


def test_doctor_with_browser_container_skips_local_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import jarvis.secrets as secrets_module
    from tests.conftest import MemoryKeyring

    backend = MemoryKeyring()
    backend.set_password("jarvis", "ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setattr(secrets_module, "_default_backend", lambda: backend)
    monkeypatch.setattr("shutil.which", lambda name: None)
    config = tmp_path / "jarvis.yaml"
    config.write_text(
        "google: {enabled: false}\nbrowser:\n  mode: remote\n  url: http://browser:8931/mcp\n", encoding="utf-8"
    )
    main(["--config", str(config), "doctor"])
    out = capsys.readouterr().out
    assert "Navegador en su contenedor" in out and "Node.js" not in out
