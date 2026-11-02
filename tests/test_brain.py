from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, ClassVar

import pytest

pytest.importorskip("claude_agent_sdk")
from callgate import ApprovalBroker, PermissionGate, PolicyContext, Redactor
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from jarvis.brain import DISALLOWED_TOOLS, Brain, build_options
from jarvis.budget import BudgetGuard
from jarvis.config import load_settings
from jarvis.security import StoreAuditSink, core_registry


def _result(**over: Any) -> ResultMessage:
    fields: dict[str, Any] = dict(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
        total_cost_usd=0.02,
        result="Hecho, Ana.",
    )
    fields.update(over)
    return ResultMessage(**fields)


class FakeClient:
    instances: ClassVar[list[FakeClient]] = []
    script: ClassVar[list[Any]] = []
    tool_calls: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, options: Any) -> None:
        self.options = options
        self.prompts: list[str] = []
        self.hook_results: list[dict[str, Any]] = []
        FakeClient.instances.append(self)

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> Any:
        hook = self.options.hooks["PreToolUse"][0].hooks[0]
        for call in FakeClient.tool_calls:
            self.hook_results.append(await hook({"tool_name": call["name"], "tool_input": call["input"]}, "id", None))
        for message in FakeClient.script:
            yield message


@pytest.fixture()
def settings(tmp_path: Path) -> Any:
    cfg = tmp_path / "jarvis.yaml"
    cfg.write_text("allowed_dirs: [work]\nbudget: {task_usd: 0.5, daily_usd: 1}\n", encoding="utf-8")
    s = load_settings(cfg)
    s.workspace.mkdir(parents=True)
    return s


@pytest.fixture()
def brain(settings: Any, store: Any, notifier: Any) -> Brain:
    FakeClient.instances = []
    FakeClient.script = [AssistantMessage(content=[TextBlock("Hecho, Ana.")], model="m"), _result()]
    FakeClient.tool_calls = []
    broker = ApprovalBroker(1)
    notifier.broker = broker
    redactor = Redactor(["sk-ant-secret-value-123456789"])

    def gate_factory(conversation_id: str) -> PermissionGate:
        ctx = PolicyContext(
            registry=core_registry(),
            allowed_dirs=settings.allowed_dirs,
            protected_paths=settings.protected_paths,
            cwd=settings.workspace,
        )
        return PermissionGate(
            context=ctx,
            redactor=redactor,
            broker=broker,
            notifier=notifier,
            audit=StoreAuditSink(store),
            conversation_id=conversation_id,
        )

    return Brain(
        settings=settings,
        store=store,
        api_key="sk-ant-secret-value-123456789",
        redactor=redactor,
        budget=BudgetGuard(store, notifier, settings.daily_budget_usd, settings.task_budget_usd),
        gate_factory=gate_factory,
        client_factory=FakeClient,
    )


def test_options_are_locked_down(settings: Any, store: Any, notifier: Any) -> None:
    gate = PermissionGate(
        context=PolicyContext(registry=core_registry(), allowed_dirs=settings.allowed_dirs),
        redactor=Redactor(),
        broker=ApprovalBroker(1),
        notifier=notifier,
        audit=StoreAuditSink(store),
    )
    options = build_options(settings, "key", gate, resume=None)
    assert options.setting_sources == []
    assert options.allowed_tools == []
    assert options.permission_mode == "default"
    assert options.strict_mcp_config is True
    assert "Bash" not in options.tools and set(DISALLOWED_TOOLS) <= set(options.disallowed_tools)
    assert options.can_use_tool is not None and options.hooks["PreToolUse"][0].matcher is None
    assert options.max_budget_usd == settings.task_budget_usd
    assert options.env["ANTHROPIC_API_KEY"] == "key"
    assert options.env["ANTHROPIC_AUTH_TOKEN"] == "" and options.env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert Path(options.env["CLAUDE_CONFIG_DIR"]).is_relative_to(settings.data_dir)
    assert "key" not in options.system_prompt
    assert options.hooks["PreToolUse"][0].timeout > settings.approval_timeout_s
    assert options.hooks["PostToolUse"][0].hooks[0].__self__.gate is gate
    assert build_options(settings, "key", gate, None, mcp_servers={"x": {"type": "stdio"}}).mcp_servers == {
        "x": {"type": "stdio"}
    }


def test_ask_persists_conversation_and_resumes(brain: Brain, store: Any) -> None:
    cid = store.create_conversation("web")
    reply = asyncio.run(brain.ask(cid, "¿Qué tengo hoy?"))
    assert reply.text == "Hecho, Ana." and reply.cost_usd == pytest.approx(0.02)
    assert [m["role"] for m in store.get_messages(cid)] == ["user", "assistant"]
    assert store.get_conversation(cid)["sdk_session_id"] == "sess-1"
    asyncio.run(brain.ask(cid, "¿Y mañana?"))
    assert FakeClient.instances[0].options.resume is None
    assert FakeClient.instances[1].options.resume == "sess-1"


def test_budget_exceeded_refuses_without_calling_model(brain: Brain, store: Any) -> None:
    run_id = store.start_run("x")
    store.finish_run(run_id, "success", 5.0, 1)
    cid = store.create_conversation("web")
    reply = asyncio.run(brain.ask(cid, "hola"))
    assert reply.status == "budget_exceeded" and FakeClient.instances == []


def test_task_budget_stop_is_explained(brain: Brain, store: Any) -> None:
    FakeClient.script = [_result(subtype="error_max_budget_usd", is_error=True, result=None)]
    reply = asyncio.run(brain.ask(store.create_conversation("web"), "haz algo caro"))
    assert "límite" in reply.text


def test_api_failures_are_reported_as_errors_so_the_queue_retries(brain: Brain, store: Any) -> None:
    # What the CLI really returns when it cannot reach the API: subtype "success" with is_error set.
    FakeClient.script = [_result(subtype="success", is_error=True, result="API Error: Connection refused")]
    reply = asyncio.run(brain.ask(store.create_conversation("web"), "hola"))
    assert reply.status == "error" and "Connection refused" in reply.text


def test_tool_calls_go_through_the_gate(brain: Brain, store: Any) -> None:
    FakeClient.tool_calls = [
        {"name": "Read", "input": {"file_path": "notas.md"}},
        {"name": "Bash", "input": {"command": "cat ~/.ssh/id_rsa"}},
    ]
    asyncio.run(brain.ask(store.create_conversation("web"), "lee mis notas"))
    decisions = [r["hookSpecificOutput"]["permissionDecision"] for r in FakeClient.instances[0].hook_results]
    assert decisions == ["allow", "deny"]
    assert {e["tool_name"] for e in store.audit_entries()} == {"Read", "Bash"}


def test_model_errors_do_not_crash(brain: Brain, store: Any) -> None:
    class Boom(FakeClient):
        async def query(self, prompt: str) -> None:
            raise RuntimeError("network down sk-ant-secret-value-123456789")

    brain._client_factory = Boom
    reply = asyncio.run(brain.ask(store.create_conversation("web"), "hola"))
    assert reply.status == "error"


def test_taint_persists_across_messages(brain: Brain, store: Any) -> None:
    cid = store.create_conversation("web")
    FakeClient.tool_calls = [{"name": "WebFetch", "input": {"url": "https://news.example.com/"}}]
    asyncio.run(brain.ask(cid, "resume esta web"))
    FakeClient.tool_calls = [{"name": "WebFetch", "input": {"url": "https://attacker.example/"}}]
    asyncio.run(brain.ask(cid, "sigue"))
    second = FakeClient.instances[1].hook_results[0]["hookSpecificOutput"]
    assert second["permissionDecision"] == "deny"  # needed approval; nobody tapped ✅
    other = store.create_conversation("web")
    asyncio.run(brain.ask(other, "otra cosa"))
    assert FakeClient.instances[2].hook_results[0]["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_fresh_run_does_not_resume_or_inherit_taint(brain: Brain, store: Any) -> None:
    cid = store.create_conversation("scheduler")
    store.kv_set(f"tainted:{cid}", "1")
    store.set_sdk_session(cid, "old-session")
    FakeClient.tool_calls = [{"name": "WebFetch", "input": {"url": "https://nuevo.example/"}}]
    asyncio.run(brain.ask(cid, "tarea programada", fresh=True, budget_usd=0.1))
    options = FakeClient.instances[0].options
    assert options.resume is None and options.max_budget_usd == 0.1
    assert FakeClient.instances[0].hook_results[0]["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_memory_is_injected_into_system_prompt(brain: Brain, store: Any) -> None:
    brain._memory_reader = lambda: "Ana prefiere reuniones por la mañana."
    asyncio.run(brain.ask(store.create_conversation("web"), "hola"))
    assert "reuniones por la mañana" in FakeClient.instances[0].options.system_prompt


def test_complete_has_no_tools_and_counts_spend(brain: Brain, store: Any) -> None:
    seen: dict[str, Any] = {}

    async def fake_query(prompt: str, options: Any) -> Any:
        seen["options"] = options
        yield _result(result='[{"id": "a", "urgent": false}]', total_cost_usd=0.004)

    brain._query_fn = fake_query
    out = asyncio.run(brain.complete("<contenido_externo>…</contenido_externo>", system="clasifica"))
    assert out.startswith("[")
    options = seen["options"]
    assert options.tools == [] and options.mcp_servers == {} and options.max_turns == 1
    assert options.model == brain._settings.fast_model and options.thinking == {"type": "disabled"}
    assert store.spent_since("2000-01-01T00:00:00+00:00") == pytest.approx(0.004)


def test_per_run_servers_and_prompt_override(brain: Brain, store: Any) -> None:
    brain.add_mcp_server("google", {"type": "stdio"})
    asyncio.run(
        brain.ask(store.create_conversation("demo"), "ronda", mcp_servers={"demo": {}}, extra_prompt="MODO DEMO")
    )
    options = FakeClient.instances[0].options
    assert options.mcp_servers == {"demo": {}} and "MODO DEMO" in options.system_prompt
    asyncio.run(brain.ask(store.create_conversation("web"), "hola"))
    assert "google" in FakeClient.instances[1].options.mcp_servers
