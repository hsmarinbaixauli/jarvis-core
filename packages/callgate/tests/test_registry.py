from __future__ import annotations

import pytest
from callgate import Effect, Tier, ToolRegistry, ToolSpec, UndeclaredToolError, mcp_parts
from callgate.claude_code import registry as claude_code_registry


def test_mcp_parts() -> None:
    assert mcp_parts("mcp__github__get_me") == ("github", "get_me")
    assert mcp_parts("mcp__play_book__x__y") == ("play_book", "x__y")
    assert mcp_parts("Read") is None and mcp_parts("mcp__only") is None


def test_require_reports_undeclared_tools() -> None:
    reg = ToolRegistry().add_server("mail", [ToolSpec("send", Effect.IRREVERSIBLE), ToolSpec("get_*", Effect.READ)])
    reg.require("mail", ["send", "get_thread"])
    with pytest.raises(UndeclaredToolError, match="delete"):
        reg.require("mail", ["send", "delete"])
    assert reg.missing("other", ["x"]) == ["x"]


def test_exact_declaration_wins_over_pattern() -> None:
    reg = ToolRegistry().add_server(
        "gh", [ToolSpec("get_*", Effect.READ), ToolSpec("get_secret_scanning", Effect.READ, tier=Tier.NOTIFY)]
    )
    resolved = reg.resolve("mcp__gh__get_secret_scanning")
    assert resolved is not None and resolved.spec is not None and resolved.spec.default_tier is Tier.NOTIFY


def test_markdown_table_lists_every_declaration() -> None:
    reg = claude_code_registry().add_server("mail", [ToolSpec("send", Effect.IRREVERSIBLE, external=False)])
    table = reg.to_markdown()
    assert "| `mcp__mail__send` | irreversible | approval | no |" in table
    assert "| `WebFetch` | read | free | yes |" in table
    assert "Bash" not in table
