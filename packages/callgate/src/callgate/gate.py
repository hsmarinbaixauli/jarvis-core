"""The gate every tool call goes through.

Order: emergency stop → secret exfiltration check → classification → approval / notice → audit →
taint tracking → observers. Framework adapters (see ``callgate.claude_agent_sdk``) only translate.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from callgate.approvals import ApprovalBroker, ApprovalRequest
from callgate.audit import AuditEntry, AuditSink
from callgate.injection import ContentInspector, response_text
from callgate.killswitch import KillSwitch
from callgate.messages import Messages
from callgate.policy import PolicyContext, classify, is_external
from callgate.redact import Redactor
from callgate.tiers import Decision, Effect, Tier

_log = logging.getLogger(__name__)

_MAX_SUMMARY = 1500


class Notifier(Protocol):
    """The human's channel: plain notices and approve/deny prompts."""

    async def notify(self, text: str) -> None: ...
    async def ask_approval(self, request: ApprovalRequest) -> None: ...


class NullNotifier:
    """No channel configured: notices go to the log and approvals are impossible (so they are denied)."""

    async def notify(self, text: str) -> None:
        _log.info("Notice (no channel): %s", text)

    async def ask_approval(self, request: ApprovalRequest) -> None:
        raise RuntimeError("No approval channel is configured.")


@dataclass
class GateOutcome:
    allowed: bool
    tier: Tier
    message: str
    effect: Effect | None = None


def summarize_input(tool_input: dict[str, Any], redactor: Redactor) -> str:
    text = json.dumps(tool_input, ensure_ascii=False, indent=1, default=str)
    text = redactor.redact(text)
    return text if len(text) <= _MAX_SUMMARY else text[:_MAX_SUMMARY] + "…"


class PermissionGate:
    def __init__(
        self,
        *,
        context: PolicyContext,
        redactor: Redactor,
        broker: ApprovalBroker,
        notifier: Notifier,
        audit: AuditSink,
        conversation_id: str | None = None,
        kill_switch: KillSwitch | None = None,
        inspector: ContentInspector | None = None,
        classifier: Callable[[str, dict[str, Any], PolicyContext], Decision] = classify,
    ) -> None:
        self.context = context
        self._redactor = redactor
        self._broker = broker
        self._notifier = notifier
        self._audit_sink = audit
        self._conversation_id = conversation_id
        self._kill_switch = kill_switch
        self._inspector = inspector
        self._classify = classifier
        self._reported: set[str] = set()

    @property
    def messages(self) -> Messages:
        return self.context.messages

    def _summary(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        summarizer = self.context.summarizers.get(tool_name)
        if summarizer is not None:
            try:
                return self._redactor.redact(summarizer(tool_input))[: _MAX_SUMMARY * 3]
            except Exception:
                _log.exception("Summarizer failed for %s", tool_name)
        return summarize_input(tool_input, self._redactor)

    def _declared_effect(self, tool_name: str) -> Effect:
        resolved = self.context.registry.resolve(tool_name)
        return resolved.spec.effect if resolved and resolved.spec else Effect.IRREVERSIBLE

    async def evaluate(self, tool_name: str, tool_input: dict[str, Any]) -> GateOutcome:
        msg = self.messages
        summary = self._summary(tool_name, tool_input)

        if self._kill_switch is not None and self._kill_switch.engaged():
            decision = Decision(Tier.BLOCK, msg("halted"), self._declared_effect(tool_name))
            self._audit(tool_name, decision, "blocked", summary)
            return GateOutcome(False, Tier.BLOCK, decision.reason, decision.effect)

        leaked = self._redactor.find(json.dumps(tool_input, ensure_ascii=False, default=str))
        if leaked:
            decision = Decision(
                Tier.BLOCK, msg("secret_reason", labels=", ".join(leaked)), self._declared_effect(tool_name)
            )
            self._audit(tool_name, decision, "blocked", summary)
            await self._safe_notify(msg("secret_notice", tool=tool_name))
            return GateOutcome(False, Tier.BLOCK, msg("secret_blocked"), decision.effect)

        decision = self._classify(tool_name, tool_input, self.context)
        effect = decision.effect

        if decision.tier is Tier.BLOCK:
            self._audit(tool_name, decision, "blocked", summary)
            return GateOutcome(False, decision.tier, decision.reason, effect)

        if decision.tier is Tier.APPROVAL:
            try:
                approved = await self._broker.request(
                    tool_name,
                    summary,
                    decision.reason,
                    self._notifier.ask_approval,
                    origin=self._conversation_id,
                    effect=effect.value if effect else None,
                )
            except Exception as exc:
                _log.warning("Approval request failed: %s", exc)
                approved = False
            recorded = self._audit(tool_name, decision, "approved" if approved else "rejected", summary)
            if not approved:
                return GateOutcome(False, decision.tier, msg("rejected"), effect)
            if not recorded:
                return GateOutcome(False, decision.tier, msg("audit_failed"), effect)
        elif decision.tier is Tier.NOTIFY:
            self._audit(tool_name, decision, "allowed", summary)
            await self._safe_notify(msg("notify", reason=decision.reason, tool=tool_name, summary=summary))
        else:
            self._audit(tool_name, decision, "allowed", summary)

        if is_external(tool_name, self.context):
            self.context.tainted = True
        for observe in self.context.observers:
            try:
                observe(tool_name, tool_input, self.context)
            except Exception:
                _log.exception("Policy observer failed for %s", tool_name)
        return GateOutcome(True, decision.tier, decision.reason, effect)

    async def after_result(self, tool_name: str, response: Any) -> str | None:
        """Feed the result to the observers and inspect external content.

        Returns the reminder to append for the model (sharper when instructions were found), or None.
        """
        for observe in self.context.result_observers:
            try:
                observe(tool_name, response, self.context)
            except Exception:
                _log.exception("Result observer failed for %s", tool_name)
        if not is_external(tool_name, self.context):
            return None
        text = response_text(response)
        finding = None
        if self._inspector is not None and text:
            try:
                finding = await self._inspector.inspect(tool_name, text)
            except Exception:
                _log.exception("Content inspector failed for %s", tool_name)
        if finding is None:
            return self.messages("external_reminder")
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if digest not in self._reported:
            self._reported.add(digest)
            reasons = "; ".join(self.messages.reason(r) for r in finding.reasons)
            excerpt = self._redactor.redact(finding.excerpt)
            decision = Decision(Tier.NOTIFY, self.messages("injection_reason", reasons=reasons), Effect.READ)
            self._audit(tool_name, decision, "flagged", excerpt)
            await self._safe_notify(self.messages("injection_notice", tool=tool_name, reasons=reasons, excerpt=excerpt))
        return self.messages("injection_reminder", reasons="; ".join(self.messages.reason(r) for r in finding.reasons))

    def _audit(self, tool_name: str, decision: Decision, verdict: str, summary: str) -> bool:
        entry = AuditEntry(
            tool_name=tool_name,
            effect=(decision.effect or Effect.IRREVERSIBLE).value,
            tier=decision.tier.value,
            verdict=verdict,
            reason=decision.reason,
            input_redacted=summary,
            conversation_id=self._conversation_id,
        )
        try:
            self._audit_sink.record(entry)
        except Exception:
            _log.exception("Could not write audit entry for %s", tool_name)
            return False
        return True

    async def _safe_notify(self, text: str) -> None:
        try:
            await self._notifier.notify(text)
        except Exception as exc:
            _log.warning("Notification failed: %s", exc)
