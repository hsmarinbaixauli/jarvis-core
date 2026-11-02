"""JarvisCore: the one API every channel uses.

Channels (Telegram, web…) call ``ask``, ``command``, ``answer`` and ``resolve_approval``, and receive
events through the bus. Approvals and questions only go to channels that can show buttons to the owner;
if none is connected, an approval is impossible and the gate denies the action.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from callgate import ApprovalBroker, ApprovalRequest

from jarvis.core.events import Channel, Event, EventBus, Question
from jarvis.core.queue import TaskChange, TaskQueue
from jarvis.scheduler import TaskSpec, has_nothing_to_report, if_relevant_prompt, missed_run
from jarvis.store import Store, utc_now

_log = logging.getLogger(__name__)

STOP_LABEL = "🛑 Parar todo"
_LAST_RUN_KEY = "task_last_run:{}"


CommandHandler = Callable[[str, str], Awaitable[str]]


class Halted(RuntimeError):
    """The emergency stop is on: nothing new runs until the owner lifts it."""


class BusNotifier:
    """callgate's Notifier on top of the event bus, so the gate, budget and watcher stay channel-agnostic."""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def notify(self, text: str) -> None:
        if await self._bus.publish(Event("notice", text)) == 0:
            _log.info("Aviso sin canal: %s", text)

    async def ask_approval(self, request: ApprovalRequest) -> None:
        if await self._bus.publish(Event("approval", approval=request)) == 0:
            raise RuntimeError("No hay ningún canal que pueda pedir aprobación (¿Telegram configurado?).")


COMMANDS: dict[str, str] = {
    "nuevo": "empezar una conversación nueva",
    "estado": "gasto de hoy, cola y aprobaciones pendientes",
    "cola": "tareas en marcha y en cola",
    "cancelar": "cancelar una tarea: /cancelar 12",
    "tareas": "tareas programadas y su próxima ejecución",
    "parar": "🛑 PARADA TOTAL: cancela todo, deniega lo pendiente y bloquea cualquier herramienta",
    "reanudar": "quitar la parada total",
    "ayuda": "esta ayuda",
}
_ALIASES = {"start": "ayuda", "help": "ayuda", "stop": "parar"}


class JarvisCore:
    def __init__(
        self,
        *,
        store: Store,
        brain: Any,
        broker: ApprovalBroker,
        kill_switch: Any,
        bus: EventBus,
        notifier: BusNotifier,
        budget: Any = None,
        max_parallel: int = 2,
    ) -> None:
        self.store = store
        self.brain = brain
        self.broker = broker
        self.kill_switch = kill_switch
        self.bus = bus
        self.notifier = notifier
        self.budget = budget
        self.scheduler: Any = None
        self._extra_commands: dict[str, tuple[str, CommandHandler]] = {}
        self._origin_labelers: list[Callable[[str], str | None]] = []
        self.queue = TaskQueue(store, brain, self, is_halted=kill_switch.engaged, max_parallel=max_parallel)
        self._questions: dict[str, Callable[[str], Awaitable[None]]] = {}
        self._background: set[asyncio.Task[Any]] = set()

    # -- channels and conversations --------------------------------------------------------

    def register(self, channel: Channel) -> None:
        self.bus.register(channel)

    def conversation_for(self, channel: str) -> str:
        key = f"conversation:{channel}"
        current = self.store.kv_get(key)
        if current and self.store.get_conversation(current):
            return current
        return self.new_conversation(channel)

    def new_conversation(self, channel: str) -> str:
        conversation_id = self.store.create_conversation(channel)
        self.store.kv_set(f"conversation:{channel}", conversation_id)
        return conversation_id

    @property
    def halted(self) -> bool:
        return bool(self.kill_switch.engaged())

    def add_origin_label(self, labeler: Callable[[str], str | None]) -> None:
        """Let an extension tag approvals that come from its own conversations (e.g. "🤖 MODO AUTÓNOMO")."""
        self._origin_labelers.append(labeler)

    def origin_label(self, origin: str | None) -> str | None:
        if origin is None:
            return None
        return next((label for labeler in self._origin_labelers if (label := labeler(origin))), None)

    def register_command(self, name: str, help_text: str, handler: CommandHandler) -> None:
        if name in COMMANDS or name in _ALIASES or name in self._extra_commands:
            raise ValueError(f"El comando /{name} ya existe.")
        self._extra_commands[name] = (help_text, handler)

    # -- asking --------------------------------------------------------------------------------

    async def ask(self, channel: str, text: str, conversation_id: str | None = None) -> dict[str, Any]:
        """Queue a message from the owner. The reply arrives as a ``reply`` event for *channel* (or via ``wait``)."""
        if self.halted:
            raise Halted("La parada total está activada. Usa /reanudar para volver a trabajar.")
        if conversation_id is None or self.store.get_conversation(conversation_id) is None:
            conversation_id = self.conversation_for(channel)
        busy = self.queue.would_wait(conversation_id)
        task = await self.queue.submit(
            kind="chat", channel=channel, conversation_id=conversation_id, prompt=text, label=text.splitlines()[0][:60]
        )
        if busy:
            await self.bus.publish(
                Event(
                    "reply",
                    f"⏳ Lo tengo en cola (tarea #{task['id']}); empiezo en cuanto acabe lo que estoy haciendo.",
                    target=channel,
                    conversation_id=conversation_id,
                )
            )
        return task

    async def wait(self, task_id: int, timeout: float | None = None) -> dict[str, Any]:
        return await self.queue.wait(task_id, timeout)

    # -- scheduled tasks ---------------------------------------------------------------------

    def _task_conversation(self, spec: TaskSpec) -> str:
        key = f"task_conversation:{spec.id}"
        existing = self.store.kv_get(key)
        if existing and self.store.get_conversation(existing):
            return existing
        conversation_id = self.store.create_conversation("scheduler", title=f"⏱ {spec.id}")
        self.store.kv_set(key, conversation_id)
        return conversation_id

    def _scheduled_fields(self, spec: TaskSpec) -> dict[str, Any]:
        return {
            "kind": "scheduled",
            "channel": "scheduler",
            "conversation_id": self._task_conversation(spec),
            "prompt": if_relevant_prompt(spec),
            "label": spec.id,
            "options": {"fresh": True, "budget_usd": spec.budget_usd, "notify": spec.notify},
        }

    async def submit_scheduled(self, spec: TaskSpec) -> dict[str, Any] | None:
        """Called by the scheduler when a task is due. Skipped (and asked about at the next start) while halted."""
        if self.halted:
            _log.info("Tarea %s omitida: parada total activada", spec.id)
            return None
        self.store.kv_set(_LAST_RUN_KEY.format(spec.id), utc_now())
        return await self.queue.submit(**self._scheduled_fields(spec))

    async def run_scheduled_now(self, spec: TaskSpec) -> dict[str, Any]:
        """Run a scheduled task inline (CLI). Created already running so another Jarvis process never picks it up."""
        if self.halted:
            raise Halted("La parada total está activada.")
        task_id = self.store.create_task(**self._scheduled_fields(spec), max_attempts=1, status="running")
        await self.queue.execute(task_id)
        task = self.store.get_task(task_id)
        assert task is not None
        return task

    async def check_missed(self, tasks: list[TaskSpec], now: datetime | None = None) -> list[str]:
        """Ask the owner about scheduled tasks that should have run while Jarvis was off. Returns their ids."""
        now = now or datetime.now(UTC)
        timezone = getattr(self.scheduler, "timezone", None)
        missed = []
        for spec in tasks:
            if not spec.enabled:
                continue
            key = _LAST_RUN_KEY.format(spec.id)
            last = self.store.kv_get(key)
            if last is None:
                self.store.kv_set(key, now.isoformat(timespec="seconds"))
                continue
            due = missed_run(spec, last, now, timezone)
            if due is None:
                continue
            missed.append(spec.id)

            async def on_answer(value: str, spec: TaskSpec = spec) -> None:
                if value == "run":
                    await self.submit_scheduled(spec)
                else:
                    self.store.kv_set(_LAST_RUN_KEY.format(spec.id), utc_now())

            when = due.astimezone(timezone).strftime("%a %d/%m %H:%M") if timezone else due.isoformat()
            await self.ask_question(
                f"⏰ Mientras estaba apagado no se ejecutó la tarea «{spec.id}» (tocaba el {when}). ¿La ejecuto ahora?",
                (("▶️ Ejecutar", "run"), ("⏭ Saltar", "skip")),
                on_answer,
            )
        return missed

    # -- questions and approvals ----------------------------------------------------------------

    async def ask_question(
        self, text: str, options: tuple[tuple[str, str], ...], on_answer: Callable[[str], Awaitable[None]]
    ) -> bool:
        question = Question(secrets.token_urlsafe(6), text, options)
        self._questions[question.id] = on_answer
        delivered = await self.bus.publish(Event("question", text, question=question))
        if not delivered:
            self._questions.pop(question.id, None)
        return delivered > 0

    async def answer(self, question_id: str, value: str) -> bool:
        handler = self._questions.pop(question_id, None)
        if handler is None:
            return False
        await handler(value)
        return True

    def resolve_approval(self, request_id: str, approved: bool) -> ApprovalRequest | None:
        return self.broker.resolve(request_id, approved)

    # -- emergency stop ---------------------------------------------------------------------------

    async def halt(self, by: str) -> str:
        self.kill_switch.engage()
        denied = self.broker.deny_all()
        cancelled = await self.queue.cancel_all()
        _log.warning("Parada total activada desde %s", by)
        return (
            "🛑 PARADA TOTAL activada.\n"
            f"Tareas canceladas: {cancelled} · aprobaciones denegadas: {denied}.\n"
            "No se ejecuta ninguna herramienta, ni tareas programadas, ni el vigilante, ni el modo autónomo.\n"
            "Sigue activa aunque reinicies. /reanudar para quitarla."
        )

    async def resume(self) -> str:
        if not self.halted:
            return "No había ninguna parada activada."
        self.kill_switch.release()
        self.queue.wake()
        return "▶️ Parada quitada. Vuelvo a trabajar (las tareas canceladas no se repiten solas)."

    # -- commands ---------------------------------------------------------------------------------

    def help_text(self) -> str:
        commands = {**COMMANDS, **{name: text for name, (text, _) in self._extra_commands.items()}}
        lines = ["Comandos de Jarvis:"] + [f"/{name} — {text}" for name, text in commands.items()]
        lines.append(f"El botón «{STOP_LABEL}» hace lo mismo que /parar.")
        lines.append("Cualquier otra cosa: háblame normal.")
        return "\n".join(lines)

    async def command(self, name: str, args: str, channel: str) -> str:
        name = _ALIASES.get(name, name)
        if name == "parar":
            return await self.halt(channel)
        if name == "reanudar":
            return await self.resume()
        if name == "nuevo":
            self.new_conversation(channel)
            return "Conversación nueva. ¿Qué necesitas?"
        if name == "estado":
            return self.status_text()
        if name == "cola":
            return self.queue_text()
        if name == "cancelar":
            if not args.strip().lstrip("#").isdigit():
                return "Dime qué tarea: /cancelar 12 (los números salen en /cola)."
            task_id = int(args.strip().lstrip("#"))
            return (
                f"Tarea #{task_id} cancelada."
                if await self.queue.cancel(task_id)
                else f"La tarea #{task_id} no está en marcha ni en cola."
            )
        if name == "tareas":
            lines = self.scheduler.describe() if self.scheduler is not None else []
            return "\n".join(lines) or "No hay tareas programadas. Añádelas en tasks.yaml."
        if name in self._extra_commands:
            return await self._extra_commands[name][1](args, channel)
        return self.help_text()

    def status_text(self) -> str:
        lines = ["🛑 PARADA TOTAL ACTIVADA (/reanudar)"] if self.halted else []
        if self.budget is not None:
            lines.append(f"Gasto de hoy: {self.budget.spent_today():.2f} $ de {self.budget.daily_usd:.2f} $")
        running = self.store.list_tasks(("running",))
        queued = self.store.list_tasks(("queued",))
        lines.append(f"Tareas: {len(running)} en marcha · {len(queued)} en cola")
        pending = self.broker.pending()
        lines.append(f"Aprobaciones pendientes: {len(pending)}")
        lines.extend(f"• {p.tool_name}: {p.reason}" for p in pending)
        return "\n".join(lines)

    def queue_text(self) -> str:
        tasks = self.store.list_tasks(("running", "queued"), limit=20)
        if not tasks:
            return "No hay nada en marcha ni en cola."
        icons = {"running": "▶️", "queued": "⏳"}
        return "\n".join(
            f"{icons[t['status']]} #{t['id']} {t['label'] or t['kind']} · intento {t['attempts']}/{t['max_attempts']}"
            for t in reversed(tasks)
        )

    def guarded(self, fn: Callable[[], Awaitable[Any]]) -> Callable[[], Awaitable[Any]]:
        """Wrap a scheduled job so it does nothing while the emergency stop is on."""

        async def run() -> Any:
            if self.halted:
                return None
            return await fn()

        return run

    def spawn(self, coro: Awaitable[Any]) -> None:
        """Run something in the background without losing the reference (extensions use it)."""
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- startup ----------------------------------------------------------------------------------

    async def startup(self, tasks: list[TaskSpec]) -> None:
        for task in self.queue.recover():
            if task["status"] == "queued":
                await self.notifier.notify(
                    f"🔁 Retomo la tarea #{task['id']} ({task['label']}), que cortó un reinicio."
                )
            else:
                await self.notifier.notify(f"⚠️ Tarea #{task['id']} ({task['label']}): {task['error']}")
        if self.halted:
            await self.notifier.notify("🛑 Sigo con la PARADA TOTAL activada. /reanudar para quitarla.")
            return
        await self.check_missed(tasks)

    # -- TaskEvents (the queue reports here) ------------------------------------------------------

    async def task_changed(self, task: dict[str, Any], change: TaskChange) -> None:
        await self.bus.publish(Event("task", task=task, extra={"change": change}))
        chat = task["kind"] == "chat"
        target = task["channel"] if chat else None
        prefix = f"⏱ {task['label']}" if not chat else ""

        async def say(text: str, kind: str = "reply" if chat else "notice") -> None:
            await self.bus.publish(Event(kind, text, target=target, conversation_id=task["conversation_id"]))  # type: ignore[arg-type]

        if change == "done":
            if chat:
                await say(task["result"])
                return
            spec = TaskSpec(task["label"], "* * * * *", task["prompt"], task["options"].get("notify", "always"))
            if not has_nothing_to_report(spec, task["result"]):
                await say(f"{prefix}\n\n{task['result']}")
        elif change == "slow" and chat:
            await say(f"⏳ Sigo con ello (tarea #{task['id']}). Te aviso al terminar; /cola para ver cómo va.")
        elif change == "retry":
            await say(
                f"🔁 Ha fallado un intento de la tarea #{task['id']}; la reintento en {task.get('retry_in_s', 0)} s."
            )
        elif change == "failed":
            await say(f"⚠️ No he podido terminar la tarea #{task['id']} {prefix}: {task['error']}".strip())
        elif change == "cancelled" and chat and not self.halted:
            await say(f"🛑 Tarea #{task['id']} cancelada.")
