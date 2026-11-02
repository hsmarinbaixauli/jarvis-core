# callgate

A permission gate for AI agent tool calls. Every call the agent makes is checked against what the tool
**declares it does**. Irreversible actions wait for a human, and every decision is written to an audit log.

It has no dependencies, keeps no I/O in its policy code, and ships an adapter for the
[Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). Any framework that lets you
intercept a tool call before it runs can use it.

## Why

An agent that reads email and browses the web can be told what to do by whoever wrote that email or
page (prompt injection). Prompt instructions alone don't fix this. You need a layer the model cannot
talk its way past:

- **Declared effects.** Each tool is `read`, `write` or `irreversible`. Irreversible tools always need
  approval. No classifier, config rule or prompt can lower that.
- **Undeclared means irreversible.** Forgetting to declare a tool costs one approval tap. It never
  becomes a silent side effect.
- **Taint tracking.** After the conversation reads third-party content (a web page, an email), outbound
  calls get stricter. For example, a fetch to an unknown domain goes from "notify" to "approve".
- **Exfiltration block.** A call whose input contains a known secret, or anything shaped like a
  credential, is blocked and reported.
- **Emergency stop.** While engaged, nothing runs.
- **Fail closed.** If an approval-tier action can't be written to the audit log, it doesn't run. If the
  approval times out, the answer is no.

## Concepts

| Effect | Default tier | Can be relaxed to | Examples |
|---|---|---|---|
| `read` | `free` | `free` | read a file, list unread email |
| `write` | `notify` | `free` | create a branch, save a draft |
| `irreversible` | `approval` | never below `approval` | send an email, merge, pay, publish, delete |

Any tool can also be `block`ed.

Tiers:
- `free`: runs silently.
- `notify`: runs, and the human gets a notice.
- `approval`: waits for an explicit yes.
- `block`: never runs.

Order of checks for each call:

1. Emergency stop.
2. Secret exfiltration.
3. Classification, in this order:
   1. declared effect;
   2. input-aware classifier;
   3. config rule;
   4. effect floor;
   5. guards, which may only tighten.
4. Approval or notice.
5. Audit.
6. Taint and observers.

## Quick start

```python
from callgate import (
    ApprovalBroker,
    Effect,
    MemoryAuditSink,
    MemoryKillSwitch,
    PermissionGate,
    PolicyContext,
    Redactor,
    ToolSpec,
)
from callgate.claude_code import registry  # Claude Code built-ins: Read, Write, WebFetch…

tools = registry().add_server(
    "mail",
    [
        ToolSpec("list_unread", Effect.READ),
        ToolSpec("send_email", Effect.IRREVERSIBLE, "Send an email."),
    ],
)
tools.require("mail", ["list_unread", "send_email"])  # fail at startup if one is undeclared

gate = PermissionGate(
    context=PolicyContext(registry=tools, allowed_dirs=(workdir,), trusted_domains=("github.com",)),
    redactor=Redactor(known_secret_values),
    broker=ApprovalBroker(timeout_s=600),
    notifier=my_channel,  # notify(text) and ask_approval(request), e.g. Telegram buttons
    audit=MemoryAuditSink(),  # or your own AuditSink (SQLite, append-only)
    kill_switch=MemoryKillSwitch(),
)

outcome = await gate.evaluate("mcp__mail__send_email", {"to": "a@example.com"})
```

The `notifier` resolves approvals by calling `broker.resolve(request.id, approved)` when the human answers.

### Claude Agent SDK

```python
from callgate.claude_agent_sdk import ClaudeAgentSDKHooks

options = ClaudeAgentOptions(
    allowed_tools=[],
    setting_sources=[],
    **ClaudeAgentSDKHooks(gate).options(approval_timeout_s=600),
)
```

Use the PreToolUse hook (included), not only `can_use_tool`: the SDK auto-allows some tools without
calling `can_use_tool`. The PostToolUse hook appends a reminder after external content.

### Input-aware rules

A server classifier sees every call to that server, declared or not. Use it for allowlists and other
input-dependent rules. Whatever it returns, the effect floor still applies.

```python
def repo_allowlist(tool, tool_input, ctx):
    if f"{tool_input.get('owner')}/{tool_input.get('repo')}" not in ALLOWED:
        return Decision(Tier.BLOCK, "Repository not allowed.")
    return None  # fall back to the declared effect


tools.add_server("github", [ToolSpec("get_*", Effect.READ), ...], classifier=repo_allowlist)
```

### Injection alerts

```python
from callgate import HeuristicInspector

gate = PermissionGate(..., inspector=HeuristicInspector())
reminder = await gate.after_result(tool_name, tool_response)  # the SDK adapter does this for you
```

After each tool whose output is external, the inspector looks for instructions aimed at the agent. It
uses English and Spanish rules: "ignore previous instructions", "you are now…", requests to send
credentials or hide things from the user, fake system markup, invisible characters.

When something is found:
- the human is notified once per content;
- the finding goes to the audit log as `flagged`;
- the reminder appended for the model becomes an explicit warning.

`ContentInspector` is a protocol, so you can put a model-based check behind the rules. Jarvis does that
to clear false alarms.

### Translation

Every text shown to the model or the human has a code (see `messages.ENGLISH`). To translate them,
pass `PolicyContext(messages=Messages({"rejected": "No aprobado.", ...}))`.

### Listing what an agent can do

`registry.to_markdown()` prints every declared tool with its effect, default tier and whether its
output counts as external content.

## What it does not do

- **Injection detection is a smoke alarm, not the wall.** The rules catch common phrasings, not every
  possible attack. What limits the damage is the gate: an instruction that slips through still can't
  make an irreversible call without the human.
- **The secret scan only covers tool inputs.** It sees known values and common token formats.
  Encoded or split secrets can get through; that is why outbound calls after external content need
  approval.
- **It doesn't sandbox the tools themselves.** A tool you mark `read` had better only read.

## Tests

```bash
pip install -e .[claude] pytest
pytest
```
