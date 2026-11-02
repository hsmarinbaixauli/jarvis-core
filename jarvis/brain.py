"""Runs one user message through the Claude Agent SDK, behind the permission gate and spend limits."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from callgate import PermissionGate, Redactor
from callgate.claude_agent_sdk import ClaudeAgentSDKHooks

from jarvis.budget import BudgetGuard
from jarvis.config import Settings
from jarvis.prompts import build_system_prompt
from jarvis.store import Store

_log = logging.getLogger(__name__)

BUILTIN_TOOLS: tuple[str, ...] = (
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
)
# Never available, whatever the model or a config file asks for.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "BashOutput",
    "KillShell",
    "KillBash",
    "Task",
    "Agent",
    "NotebookEdit",
    "Skill",
    "SlashCommand",
    "CronCreate",
    "CronDelete",
    "EnterWorktree",
    "RemoteTrigger",
)
_MAX_USER_CHARS = 8000
# Other ways Claude Code can authenticate. Blanked so the agent can only bill the API key in the
# keyring, never the owner's subscription login or a stray token in their environment.
_COMPETING_AUTH_ENV: tuple[str, ...] = (
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


def agent_env(settings: Settings, api_key: str) -> dict[str, str]:
    config_dir = settings.data_dir / "claude-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    env = dict.fromkeys(_COMPETING_AUTH_ENV, "")
    env.update({"ANTHROPIC_API_KEY": api_key, "CLAUDE_CONFIG_DIR": str(config_dir)})
    return env


@dataclass
class BrainReply:
    text: str
    cost_usd: float
    status: str
    num_turns: int = 0


def _taint_key(conversation_id: str) -> str:
    return f"tainted:{conversation_id}"


def build_options(
    settings: Settings,
    api_key: str,
    gate: PermissionGate,
    resume: str | None,
    stderr: Callable[[str], None] | None = None,
    mcp_servers: dict[str, Any] | None = None,
    memory_text: str = "",
    budget_usd: float | None = None,
    extra_prompt: str = "",
) -> Any:
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        tools=list(BUILTIN_TOOLS),
        allowed_tools=[],
        disallowed_tools=list(DISALLOWED_TOOLS),
        permission_mode="default",
        setting_sources=[],
        strict_mcp_config=True,
        mcp_servers=dict(mcp_servers or {}),
        system_prompt=build_system_prompt(
            settings.allowed_dirs, memory_text, extra_prompt, settings.owner_name, settings.owner_about
        ),
        model=settings.model,
        max_turns=settings.max_turns,
        max_budget_usd=budget_usd or settings.task_budget_usd,
        cwd=str(settings.workspace),
        add_dirs=[str(d) for d in settings.allowed_dirs[1:]],
        env=agent_env(settings, api_key),
        **ClaudeAgentSDKHooks(gate).options(settings.approval_timeout_s),
        resume=resume,
        stderr=stderr,
    )


class Brain:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        api_key: str,
        redactor: Redactor,
        budget: BudgetGuard,
        gate_factory: Callable[[str], PermissionGate],
        client_factory: Callable[..., Any] | None = None,
        mcp_servers: dict[str, Any] | None = None,
        memory_reader: Callable[[], str] | None = None,
        extra_prompt: str = "",
    ) -> None:
        self._settings = settings
        self._store = store
        self._api_key = api_key
        self._redactor = redactor
        self._budget = budget
        self._gate_factory = gate_factory
        self._client_factory = client_factory
        self._locks: dict[str, asyncio.Lock] = {}
        self._mcp_servers = dict(mcp_servers or {})
        self._memory_reader = memory_reader
        self.extra_prompt = extra_prompt
        self._query_fn: Callable[..., Any] | None = None

    def add_mcp_server(self, name: str, config: Any) -> None:
        self._mcp_servers[name] = config

    async def ask(
        self,
        conversation_id: str,
        text: str,
        *,
        fresh: bool = False,
        budget_usd: float | None = None,
        mcp_servers: dict[str, Any] | None = None,
        extra_prompt: str | None = None,
    ) -> BrainReply:
        """Run *text* in a conversation. *fresh* starts a new SDK session (scheduled tasks)."""
        text = text.strip()[:_MAX_USER_CHARS]
        refusal = self._budget.refusal_reason()
        if refusal:
            return BrainReply(refusal, 0.0, "budget_exceeded")
        conversation = self._store.get_conversation(conversation_id)
        if conversation is None:
            raise KeyError(conversation_id)

        lock = self._locks.setdefault(conversation_id, asyncio.Lock())
        async with lock:
            self._store.add_message(conversation_id, "user", text)
            self._store.set_title_if_empty(conversation_id, text.splitlines()[0] if text else "")
            run_id = self._store.start_run(conversation_id)
            try:
                resume = None if fresh else conversation.get("sdk_session_id")
                reply = await self._run(conversation_id, text, resume, budget_usd, mcp_servers, extra_prompt)
            except Exception as exc:
                _log.error("Brain run failed: %s", self._redactor.redact(str(exc)))
                self._store.finish_run(run_id, "error", 0.0, 0)
                reply = BrainReply("Ha fallado algo al procesar eso; lo tienes en el log.", 0.0, "error")
                self._store.add_message(conversation_id, "assistant", reply.text)
                return reply
            self._store.finish_run(run_id, reply.status, reply.cost_usd, reply.num_turns)
            self._store.add_message(conversation_id, "assistant", reply.text)
        await self._budget.after_run(reply.cost_usd)
        return reply

    async def complete(self, prompt: str, *, system: str, model: str | None = None, budget_usd: float = 0.05) -> str:
        """One tool-less model call (e.g. triaging emails). Counted in the daily spend like any run."""
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

        if self._budget.refusal_reason():
            raise RuntimeError("Límite de gasto diario alcanzado.")

        async def deny_all(hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "Sin herramientas en esta tarea.",
                }
            }

        options = ClaudeAgentOptions(
            tools=[],
            allowed_tools=[],
            disallowed_tools=list(DISALLOWED_TOOLS),
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={},
            system_prompt=system,
            model=model or self._settings.fast_model,
            max_turns=1,
            max_budget_usd=budget_usd,
            # The CLI thinks by default: ~35 s for a 110-word email. Short structured calls don't need it.
            thinking={"type": "disabled"},
            cwd=str(self._settings.workspace),
            env=agent_env(self._settings, self._api_key),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[deny_all])]},
            stderr=self._log_stderr,
        )
        run_id = self._store.start_run("system")
        result: Any = None
        try:
            run_query = self._query_fn or query
            async for message in run_query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        finally:
            cost = float(getattr(result, "total_cost_usd", 0.0) or 0.0)
            self._store.finish_run(run_id, getattr(result, "subtype", "error"), cost, 1)
            await self._budget.after_run(cost)
        if result is None or result.is_error:
            raise RuntimeError("El modelo no devolvió respuesta.")
        return (result.result or "").strip()

    async def run_subagent(
        self,
        *,
        prompt: str,
        cwd: Path,
        account: str,
        tools: tuple[str, ...] = ("Write", "Edit", "Read", "Glob"),
        budget_usd: float = 1.0,
        daily_limit_usd: float | None = None,
        model: str | None = None,
        max_turns: int = 25,
    ) -> dict[str, Any]:
        """Run a focused agent confined to *cwd* (no web, no shell), e.g. to build a demo site.

        Spend is recorded under *account* so a playbook can have its own daily limit.
        """
        from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

        from jarvis.budget import start_of_local_day_utc
        from jarvis.playbooks import subagent_hook

        if self._budget.refusal_reason():
            raise RuntimeError("Límite de gasto diario de Jarvis alcanzado.")
        if daily_limit_usd is not None:
            spent = self._store.spent_since_for(account, start_of_local_day_utc())
            if spent + budget_usd > daily_limit_usd:
                raise RuntimeError(f"Límite diario de este playbook: llevas {spent:.2f} $ de {daily_limit_usd:.2f} $.")
        cwd.mkdir(parents=True, exist_ok=True)

        async def hook(hook_input: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
            return await subagent_hook(cwd, hook_input)

        options = ClaudeAgentOptions(
            tools=[t for t in tools if t in ("Write", "Edit", "Read", "Glob", "Grep")],
            allowed_tools=[],
            disallowed_tools=list(DISALLOWED_TOOLS),
            permission_mode="default",
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={},
            model=model or self._settings.model,
            max_turns=max_turns,
            max_budget_usd=budget_usd,
            cwd=str(cwd),
            env=agent_env(self._settings, self._api_key),
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[hook])]},
            stderr=self._log_stderr,
        )
        run_id = self._store.start_run(account)
        result: Any = None
        try:
            run_query = self._query_fn or query
            async for message in run_query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
        finally:
            cost = float(getattr(result, "total_cost_usd", 0.0) or 0.0)
            self._store.finish_run(
                run_id, getattr(result, "subtype", "error"), cost, int(getattr(result, "num_turns", 0) or 0)
            )
            await self._budget.after_run(cost)
        if result is None:
            raise RuntimeError("El subagente no devolvió resultado.")
        return {
            "ok": not result.is_error,
            "result": (result.result or "").strip(),
            "cost_usd": cost,
            "status": result.subtype,
        }

    async def _run(
        self,
        conversation_id: str,
        text: str,
        resume: str | None,
        budget_usd: float | None = None,
        mcp_servers: dict[str, Any] | None = None,
        extra_prompt: str | None = None,
    ) -> BrainReply:
        from claude_agent_sdk import AssistantMessage, ClaudeSDKClient, ResultMessage, TextBlock

        gate = self._gate_factory(conversation_id)
        # Third-party content read earlier stays in the resumed context, so the taint must outlive the run.
        if resume is not None:
            gate.context.tainted = gate.context.tainted or self._store.kv_get(_taint_key(conversation_id)) == "1"
        options = build_options(
            self._settings,
            self._api_key,
            gate,
            resume,
            stderr=self._log_stderr,
            mcp_servers=self._mcp_servers if mcp_servers is None else mcp_servers,
            memory_text=self._read_memory(),
            budget_usd=budget_usd,
            extra_prompt=self.extra_prompt if extra_prompt is None else extra_prompt,
        )
        factory = self._client_factory or ClaudeSDKClient
        texts: list[str] = []
        result: Any = None
        try:
            async with factory(options=options) as client:
                await client.query(text)
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
                    elif isinstance(message, ResultMessage):
                        result = message
        finally:
            self._store.kv_set(_taint_key(conversation_id), "1" if gate.context.tainted else "0")

        if result is None:
            return BrainReply("No he recibido respuesta del modelo.", 0.0, "error")
        if result.session_id:
            self._store.set_sdk_session(conversation_id, result.session_id)
        cost = float(result.total_cost_usd or 0.0)
        body = (result.result or "").strip() or "\n\n".join(t.strip() for t in texts if t.strip())
        if result.subtype == "error_max_budget_usd":
            limit = budget_usd or self._settings.task_budget_usd
            body = (body + "\n\n" if body else "") + f"⛔ Paro aquí: esta tarea ha llegado al límite de {limit:.2f} $."
        elif result.subtype == "error_max_turns":
            body = (body + "\n\n" if body else "") + "⛔ Paro aquí: la tarea necesitaba demasiados pasos."
        elif result.is_error and not body:
            body = "El modelo devolvió un error; inténtalo de nuevo en un momento."
        # The CLI reports API failures (network, overload) as subtype "success" with is_error set; the queue
        # needs to see them as errors to retry them.
        status = "error" if result.is_error and result.subtype == "success" else result.subtype
        return BrainReply(body or "Hecho.", cost, status, int(result.num_turns or 0))

    def _read_memory(self) -> str:
        if self._memory_reader is None:
            return ""
        try:
            return self._memory_reader()
        except Exception:
            _log.exception("Could not read the memory file")
            return ""

    def _log_stderr(self, line: str) -> None:
        _log.debug("claude: %s", self._redactor.redact(line))
