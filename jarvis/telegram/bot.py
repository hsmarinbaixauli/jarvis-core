"""Telegram channel: the owner's control panel from the phone. Long polling, so no open ports are needed.

A thin adapter over JarvisCore: messages and commands go to the core, events come back as messages.
Only the configured chat id is served; every other chat is ignored. It is the channel that shows
approvals and questions as buttons, and it keeps a persistent "stop everything" button.
With a Voicebox configured, voice notes are transcribed into plain messages and their replies also come
back as voice notes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

import httpx

from jarvis.core.events import Event
from jarvis.core.service import STOP_LABEL, Halted, JarvisCore
from jarvis.security import EFFECT_LABELS
from jarvis.telegram.client import TelegramClient, TelegramError, inline_keyboard
from jarvis.voice import VoiceboxClient, VoiceBusy, VoiceError, speakable

_log = logging.getLogger(__name__)

_OFFSET_KEY = "telegram_offset"
CHANNEL = "telegram"
STOP_KEYBOARD = {"keyboard": [[{"text": STOP_LABEL}]], "resize_keyboard": True, "is_persistent": True}


class TelegramBot:
    name = CHANNEL
    can_approve = True

    def __init__(
        self, client: TelegramClient, core: JarvisCore, chat_id: int | None, voice: VoiceboxClient | None = None
    ) -> None:
        self.client = client
        self.core = core
        self.chat_id = chat_id
        self.voice = voice
        # Chat tasks that came in as a voice note: their reply is also spoken.
        self._voice_tasks: set[int] = set()
        self._background: set[asyncio.Task[Any]] = set()

    # -- Channel: events from the core -------------------------------------------------------

    async def deliver(self, event: Event) -> None:
        if self.chat_id is None:
            if event.kind in ("approval", "question"):
                raise RuntimeError("Falta telegram.allowed_chat_id en jarvis.yaml.")
            return
        if event.kind in ("reply", "notice"):
            await self.client.send_message(self.chat_id, event.text)
        elif event.kind == "approval" and event.approval is not None:
            await self._send_approval(event)
        elif event.kind == "question" and event.question is not None:
            q = event.question
            await self.client.send_message(
                self.chat_id, q.text, inline_keyboard([[(label, f"q:{q.id}:{value}") for label, value in q.options]])
            )
        elif event.kind == "task" and event.task is not None:
            task, change = event.task, event.extra.get("change")
            if change == "running" and task["kind"] == "chat" and task["channel"] == CHANNEL:
                await self.client.send_typing(self.chat_id)
            elif task["id"] in self._voice_tasks and change in ("done", "failed", "cancelled"):
                self._voice_tasks.discard(task["id"])
                if change == "done" and task.get("result"):
                    self._spawn(self._speak(str(task["result"])))

    async def _send_approval(self, event: Event) -> None:
        request = event.approval
        assert request is not None and self.chat_id is not None
        buttons = inline_keyboard([[("✅ Aprobar", f"ap:{request.id}:y"), ("❌ Rechazar", f"ap:{request.id}:n")]])
        minutes = max(1, int(self.core.broker.timeout_s // 60))
        label = self.core.origin_label(request.origin)
        effect = f" · {EFFECT_LABELS.get(request.effect, request.effect)}" if request.effect else ""
        text = (
            (f"{label} · " if label else "") + f"🔐 Necesito tu aprobación{effect}\n{request.reason}"
            f"\n\nHerramienta: {request.tool_name}\n{request.summary}"
            f"\n\nSi no respondes en {minutes} min, lo cancelo."
        )
        await self.client.send_message(self.chat_id, text, buttons)

    async def say(self, text: str, with_stop_button: bool = False) -> None:
        if self.chat_id is None:
            return
        try:
            await self.client.send_message(self.chat_id, text, STOP_KEYBOARD if with_stop_button else None)
        except TelegramError as exc:
            _log.warning("Telegram send failed: %s", exc)

    # -- polling -------------------------------------------------------------------------------

    async def run_forever(self) -> None:
        store = self.core.store
        offset = int(store.kv_get(_OFFSET_KEY, "0") or 0)
        _log.info("Telegram polling (chat permitido: %s)", self.chat_id or "sin configurar")
        while True:
            try:
                updates = await self.client.get_updates(offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning("getUpdates failed: %s — retrying in 10 s", exc)
                await asyncio.sleep(10)
                continue
            for update in updates:
                offset = max(offset, int(update["update_id"]) + 1)
                store.kv_set(_OFFSET_KEY, str(offset))
                try:
                    await self.handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _log.exception("Error handling update %s", update.get("update_id"))

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = (cq.get("message") or {}).get("chat", {}).get("id")
            sender = (cq.get("from") or {}).get("id")
            if self.chat_id is None or chat != self.chat_id or sender != self.chat_id:
                _log.warning("Ignoring callback from unauthorised chat %s", chat)
                return
            await self._handle_callback(cq)
            return

        msg = update.get("message") or {}
        chat = msg.get("chat", {}).get("id")
        text = (msg.get("text") or "").strip()
        voice_note = msg.get("voice") or msg.get("audio")
        if chat is None or not (text or voice_note):
            return
        if self.chat_id is None:
            await self.client.send_message(
                chat,
                f"Tu chat id es {chat}. Ponlo en jarvis.yaml (telegram.allowed_chat_id) y reinicia Jarvis.",
            )
            return
        if chat != self.chat_id:
            _log.warning("Ignoring message from unauthorised chat %s", chat)
            return

        spoken = False
        if not text and voice_note:
            text = await self._transcribe(voice_note) or ""
            if not text:
                return
            spoken = True
        elif text == STOP_LABEL:
            await self.say(await self.core.command("parar", "", CHANNEL))
            return
        if text.startswith("/") and not spoken:
            head, _, args = text.partition(" ")
            name = head[1:].split("@")[0].lower()
            await self.say(await self.core.command(name, args, CHANNEL), with_stop_button=name in ("start", "ayuda"))
            return
        try:
            task = await self.core.ask(CHANNEL, text)
        except Halted as exc:
            await self.say(f"🛑 {exc}")
            return
        if spoken and self.voice is not None and self.voice.settings.reply_with_voice:
            self._voice_tasks.add(int(task["id"]))

    # -- voice ---------------------------------------------------------------------------------

    async def _transcribe(self, note: dict[str, Any]) -> str | None:
        """The text of a voice note, shown back to the owner so a misheard word is visible before it runs."""
        if self.voice is None:
            await self.say("🎙️ No tengo la voz activada (sección voice en jarvis.yaml). Escríbeme, por favor.")
            return None
        limit = self.voice.settings.max_seconds
        if int(note.get("duration") or 0) > limit:
            await self.say(f"🎙️ La nota dura más de {limit} s; mándamela más corta o escríbemela.")
            return None
        try:
            await self.client.send_typing(self.chat_id)  # type: ignore[arg-type]
            audio = await self.client.download_file(str(note["file_id"]))
            text = await self.voice.transcribe(audio, str(note.get("file_name") or "nota.ogg"))
        except VoiceBusy as exc:
            await self.say(f"🎙️ {exc}")
            return None
        except (VoiceError, TelegramError, httpx.HTTPError, KeyError, OSError) as exc:
            _log.warning("Voice note failed: %s", exc)
            await self.say("🎙️ No he podido procesar la nota de voz (¿está Voicebox abierto?). Escríbemela.")
            return None
        if not text:
            await self.say("🎙️ No he entendido nada en la nota de voz.")
            return None
        await self.say(f"🎙️ «{text}»")
        return text

    async def _speak(self, text: str) -> None:
        assert self.voice is not None and self.chat_id is not None
        clean = speakable(text)
        if not clean or len(clean) > self.voice.settings.max_reply_chars:
            return
        try:
            await self.client.send_voice(self.chat_id, await self.voice.speak(clean))
        except VoiceBusy as exc:
            await self.say(f"🎙️ {exc}")
        except (VoiceError, TelegramError, httpx.HTTPError, OSError) as exc:
            _log.warning("Voice reply failed: %s", exc)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _handle_callback(self, cq: dict[str, Any]) -> None:
        kind, _, rest = (cq.get("data") or "").partition(":")
        target_id, _, answer = rest.rpartition(":")
        msg = cq.get("message") or {}
        if kind == "ap" and answer in ("y", "n") and target_id:
            approved = answer == "y"
            req = self.core.resolve_approval(target_id, approved)
            await self.client.answer_callback(
                cq["id"], ("Aprobado" if approved else "Rechazado") if req else "Esa petición ya no está pendiente."
            )
            verdict = ("✅ Aprobado" if approved else "❌ Rechazado") if req else "⌛ Ya no estaba pendiente"
        elif kind == "q" and target_id and answer:
            answered = await self.core.answer(target_id, answer)
            await self.client.answer_callback(cq["id"], "Hecho" if answered else "Esa pregunta ya no está pendiente.")
            verdict = "✓ Respondido" if answered else "⌛ Ya no estaba pendiente"
        else:
            await self.client.answer_callback(cq["id"], "Acción no válida")
            return
        if msg.get("message_id"):
            await self.client.clear_buttons(msg["chat"]["id"], msg["message_id"])
            await self.client.edit_text(msg["chat"]["id"], msg["message_id"], f"{msg.get('text', '')}\n\n{verdict}")
