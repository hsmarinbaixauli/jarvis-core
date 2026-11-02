"""Adapter for the Claude Agent SDK: hooks and ``can_use_tool`` that route every call through the gate.

Use the PreToolUse hook, not only ``can_use_tool``: the SDK auto-allows some tools without asking
``can_use_tool``, but every call reaches the hook. ``can_use_tool`` stays as a fallback.
"""

from __future__ import annotations

from typing import Any

from callgate.gate import PermissionGate


class ClaudeAgentSDKHooks:
    def __init__(self, gate: PermissionGate) -> None:
        self.gate = gate

    async def pre_tool_use(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        outcome = await self.gate.evaluate(str(hook_input.get("tool_name", "")), hook_input.get("tool_input") or {})
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if outcome.allowed else "deny",
                "permissionDecisionReason": outcome.message,
            }
        }

    async def post_tool_use(self, hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        reminder = await self.gate.after_result(str(hook_input.get("tool_name", "")), hook_input.get("tool_response"))
        if reminder is None:
            return {}
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": reminder}}

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

        outcome = await self.gate.evaluate(tool_name, tool_input)
        if outcome.allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(message=outcome.message)

    def options(self, approval_timeout_s: float) -> dict[str, Any]:
        """Keyword arguments for ``ClaudeAgentOptions``. The hook must outlive the approval wait."""
        from claude_agent_sdk import HookMatcher

        return {
            "hooks": {
                "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use], timeout=approval_timeout_s + 60)],
                # Inspecting a result may call a model (a few seconds); leave it room.
                "PostToolUse": [HookMatcher(matcher=None, hooks=[self.post_tool_use], timeout=120)],
            },
            "can_use_tool": self.can_use_tool,
        }
