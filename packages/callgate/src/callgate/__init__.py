"""callgate: a permission gate for AI agent tool calls."""

from callgate.approvals import ApprovalBroker, ApprovalRequest
from callgate.audit import AuditEntry, AuditSink, MemoryAuditSink
from callgate.gate import GateOutcome, NullNotifier, PermissionGate, summarize_input
from callgate.gate import Notifier as Notifier
from callgate.injection import ContentInspector, Finding, HeuristicInspector, response_text
from callgate.killswitch import KillSwitch, MemoryKillSwitch
from callgate.messages import ENGLISH, Messages
from callgate.policy import (
    Observer,
    PolicyContext,
    ResultObserver,
    ToolRule,
    check_path,
    classify,
    classify_url,
    domain_matches,
    host_is_trusted,
    is_external,
    is_within,
)
from callgate.redact import RedactingFilter, Redactor
from callgate.registry import Classifier, ToolRegistry, ToolSpec, UndeclaredToolError, mcp_parts
from callgate.tiers import Decision, Effect, Tier, stricter

__all__ = [
    "ENGLISH",
    "ApprovalBroker",
    "ApprovalRequest",
    "AuditEntry",
    "AuditSink",
    "Classifier",
    "ContentInspector",
    "Decision",
    "Effect",
    "Finding",
    "GateOutcome",
    "HeuristicInspector",
    "KillSwitch",
    "MemoryAuditSink",
    "MemoryKillSwitch",
    "Messages",
    "Notifier",
    "NullNotifier",
    "Observer",
    "PermissionGate",
    "PolicyContext",
    "RedactingFilter",
    "Redactor",
    "ResultObserver",
    "Tier",
    "ToolRegistry",
    "ToolRule",
    "ToolSpec",
    "UndeclaredToolError",
    "check_path",
    "classify",
    "classify_url",
    "domain_matches",
    "host_is_trusted",
    "is_external",
    "is_within",
    "mcp_parts",
    "response_text",
    "stricter",
    "summarize_input",
]

__version__ = "0.1.0"
