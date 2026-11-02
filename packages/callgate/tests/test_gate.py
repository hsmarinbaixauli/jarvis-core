from __future__ import annotations

import asyncio
from typing import Any

import pytest
from callgate import (
    ApprovalBroker,
    Effect,
    MemoryAuditSink,
    MemoryKillSwitch,
    Messages,
    NullNotifier,
    PermissionGate,
    PolicyContext,
    Redactor,
    Tier,
    ToolSpec,
)
from callgate.claude_agent_sdk import ClaudeAgentSDKHooks

SECRET = "sk-ant-api03-SUPERSECRETVALUE-1234567890abcdef"


class BrokenAudit:
    def record(self, entry: Any) -> None:
        raise OSError("disk full")


def make_gate(
    ctx: PolicyContext,
    notifier: Any,
    timeout: float = 2.0,
    audit: Any = None,
    kill_switch: Any = None,
) -> tuple[PermissionGate, ApprovalBroker, Any]:
    ctx.registry.add_server(
        "mail",
        [
            ToolSpec("list_unread", Effect.READ),
            ToolSpec("send_email", Effect.IRREVERSIBLE),
            ToolSpec("draft", Effect.WRITE),
        ],
    )
    broker = ApprovalBroker(timeout)
    notifier.broker = broker
    sink = audit if audit is not None else MemoryAuditSink()
    gate = PermissionGate(
        context=ctx,
        redactor=Redactor([SECRET]),
        broker=broker,
        notifier=notifier,
        audit=sink,
        conversation_id="c1",
        kill_switch=kill_switch,
    )
    return gate, broker, sink


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_free_tool_is_allowed_and_audited(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, audit = make_gate(ctx, notifier)
    outcome = run(gate.evaluate("Read", {"file_path": "a.md"}))
    assert outcome.allowed and outcome.tier is Tier.FREE
    entry = audit.entries[0]
    assert (entry.verdict, entry.effect, entry.conversation_id) == ("allowed", "read", "c1")
    assert notifier.notices == []


def test_write_tier_notifies(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, _ = make_gate(ctx, notifier)
    assert run(gate.evaluate("mcp__mail__draft", {"to": "a@b.es"})).tier is Tier.NOTIFY
    assert len(notifier.notices) == 1 and "mcp__mail__draft" in notifier.notices[0]


def test_irreversible_needs_approval_and_carries_its_effect(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, audit = make_gate(ctx, notifier)
    notifier.auto_answer = True
    outcome = run(gate.evaluate("mcp__mail__send_email", {"to": "a@b.es"}))
    assert outcome.allowed and outcome.effect is Effect.IRREVERSIBLE
    assert notifier.approval_requests[0].effect == "irreversible"
    assert audit.entries[0].verdict == "approved"


def test_approval_rejected(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, audit = make_gate(ctx, notifier)
    notifier.auto_answer = False
    outcome = run(gate.evaluate("mcp__mail__send_email", {"to": "a@b.es"}))
    assert not outcome.allowed and "did not approve" in outcome.message
    assert (audit.entries[0].verdict, audit.entries[0].tier) == ("rejected", "approval")


def test_approval_times_out_as_denied(ctx: PolicyContext, notifier: Any) -> None:
    gate, broker, _ = make_gate(ctx, notifier, timeout=0.05)
    assert not run(gate.evaluate("mcp__mail__send_email", {})).allowed
    assert broker.pending() == []


def test_approval_impossible_without_a_channel(ctx: PolicyContext) -> None:
    gate, _, _ = make_gate(ctx, NullNotifier())
    assert not run(gate.evaluate("mcp__mail__send_email", {})).allowed


def test_secret_in_input_is_blocked_and_reported(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, audit = make_gate(ctx, notifier)
    outcome = run(gate.evaluate("WebFetch", {"url": f"https://github.com/?k={SECRET}"}))
    assert not outcome.allowed and outcome.tier is Tier.BLOCK
    assert any("secret" in n for n in notifier.notices)
    assert SECRET not in audit.entries[0].input_redacted and audit.entries[0].verdict == "blocked"


def test_secret_pattern_blocked_even_if_unknown(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, _ = make_gate(ctx, notifier)
    assert not run(gate.evaluate("Write", {"file_path": "a.txt", "content": "ghp_" + "A" * 36})).allowed


def test_external_content_taints_following_calls(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, _ = make_gate(ctx, notifier, timeout=0.05)
    assert run(gate.evaluate("mcp__mail__list_unread", {})).allowed
    assert ctx.tainted
    second = run(gate.evaluate("WebFetch", {"url": "https://attacker.example/"}))
    assert second.tier is Tier.APPROVAL and not second.allowed


def test_own_data_does_not_taint(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, _ = make_gate(ctx, notifier)
    run(gate.evaluate("Read", {"file_path": "a.md"}))
    assert not ctx.tainted


def test_approval_fails_closed_when_audit_unavailable(ctx: PolicyContext, notifier: Any) -> None:
    gate, _, _ = make_gate(ctx, notifier, audit=BrokenAudit())
    notifier.auto_answer = True
    outcome = run(gate.evaluate("mcp__mail__send_email", {}))
    assert not outcome.allowed and "audit" in outcome.message


def test_kill_switch_blocks_everything_without_asking(ctx: PolicyContext, notifier: Any) -> None:
    switch = MemoryKillSwitch(engaged=True)
    gate, _, audit = make_gate(ctx, notifier, kill_switch=switch)
    notifier.auto_answer = True
    for name, tool_input in (("Read", {"file_path": "a.md"}), ("mcp__mail__send_email", {})):
        outcome = run(gate.evaluate(name, tool_input))
        assert not outcome.allowed and "emergency stop" in outcome.message
    assert notifier.approval_requests == [] and [e.verdict for e in audit.entries] == ["blocked", "blocked"]
    switch.release()
    assert run(gate.evaluate("Read", {"file_path": "a.md"})).allowed


def test_messages_are_translatable(ctx: PolicyContext, notifier: Any) -> None:
    ctx.messages = Messages({"rejected": "No aprobado."})
    gate, _, _ = make_gate(ctx, notifier)
    notifier.auto_answer = False
    assert run(gate.evaluate("mcp__mail__send_email", {})).message == "No aprobado."


def test_after_result_reminds_only_for_external_content(ctx: PolicyContext, notifier: Any) -> None:
    seen: list[str] = []
    ctx.result_observers = (lambda name, response, c: seen.append(name),)
    gate, _, _ = make_gate(ctx, notifier)
    assert asyncio.run(gate.after_result("mcp__mail__list_unread", "x")) is not None
    assert asyncio.run(gate.after_result("Read", "x")) is None
    assert seen == ["mcp__mail__list_unread", "Read"]


def test_sdk_hook_output_shape(ctx: PolicyContext, notifier: Any) -> None:
    hooks = ClaudeAgentSDKHooks(make_gate(ctx, notifier)[0])
    allowed = run(hooks.pre_tool_use({"tool_name": "Read", "tool_input": {"file_path": "a"}}, "t1", None))
    denied = run(hooks.pre_tool_use({"tool_name": "Bash", "tool_input": {"command": "ls"}}, "t2", None))
    assert allowed["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    post = run(hooks.post_tool_use({"tool_name": "WebFetch", "tool_response": "page"}, "t3", None))
    assert "external content" in post["hookSpecificOutput"]["additionalContext"]
    assert run(hooks.post_tool_use({"tool_name": "Read", "tool_response": "x"}, "t4", None)) == {}


def test_sdk_can_use_tool(ctx: PolicyContext, notifier: Any) -> None:
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    hooks = ClaudeAgentSDKHooks(make_gate(ctx, notifier)[0])
    assert isinstance(run(hooks.can_use_tool("Read", {"file_path": "a"}, None)), PermissionResultAllow)
    denied = run(hooks.can_use_tool("Bash", {"command": "ls"}, None))
    assert isinstance(denied, PermissionResultDeny) and denied.message
    assert set(hooks.options(60)) == {"hooks", "can_use_tool"}
