"""Minimal async Telegram Bot API client (long polling, no public URL needed)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

_log = logging.getLogger(__name__)

_MAX_TEXT = 4000  # Telegram hard limit is 4096


class TelegramError(RuntimeError):
    pass


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    """[[("✅ Enviar", "approve:3"), ...]] → reply_markup dict."""
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def split_text(text: str, limit: int = _MAX_TEXT) -> list[str]:
    text = text or "(vacío)"
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        cut = cut if cut > limit // 2 else limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    chunks.append(text)
    return chunks


class TelegramClient:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        self._file_base = f"https://api.telegram.org/file/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        resp = await self._http.post(f"{self._base}/{method}", json=params)
        try:
            data = resp.json()
        except ValueError as exc:
            raise TelegramError(f"{method}: respuesta no JSON (HTTP {resp.status_code})") from exc
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description', resp.text[:200])}")
        return data["result"]

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        # The token is part of every URL: never log request URLs (see app logging setup).
        return await self.call(
            "getUpdates", offset=offset, timeout=timeout, allowed_updates=["message", "callback_query"]
        )

    async def send_message(
        self, chat_id: int, text: str, buttons: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        last = None
        chunks = split_text(text)
        for i, chunk in enumerate(chunks):
            params: dict[str, Any] = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = buttons
            last = await self.call("sendMessage", **params)
        return last

    async def send_photo(self, chat_id: int, path: str, caption: str = "") -> None:
        file_path = Path(path)
        with file_path.open("rb") as fh:
            resp = await self._http.post(
                f"{self._base}/sendPhoto",
                data={"chat_id": str(chat_id), "caption": caption[:1000]},
                files={"photo": (file_path.name, fh, "image/png")},
            )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"sendPhoto: {data.get('description')}")

    async def download_file(self, file_id: str, max_bytes: int = 20_000_000) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        if int(info.get("file_size") or 0) > max_bytes or not info.get("file_path"):
            raise TelegramError("getFile: archivo demasiado grande o no disponible")
        resp = await self._http.get(f"{self._file_base}/{info['file_path']}")
        if resp.status_code != 200:
            raise TelegramError(f"descarga de archivo: HTTP {resp.status_code}")
        return resp.content

    async def send_voice(self, chat_id: int, ogg: bytes, caption: str = "") -> None:
        resp = await self._http.post(
            f"{self._base}/sendVoice",
            data={"chat_id": str(chat_id), "caption": caption[:1000]},
            files={"voice": ("jarvis.ogg", ogg, "audio/ogg")},
        )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(f"sendVoice: {data.get('description')}")

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        try:
            await self.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text[:_MAX_TEXT],
                disable_web_page_preview=True,
            )
        except TelegramError as exc:
            _log.debug("edit_text: %s", exc)

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
        except TelegramError as exc:
            _log.debug("send_typing: %s", exc)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    async def clear_buttons(self, chat_id: int, message_id: int) -> None:
        try:
            await self.call(
                "editMessageReplyMarkup",
                chat_id=chat_id,
                message_id=message_id,
                reply_markup={"inline_keyboard": []},
            )
        except TelegramError as exc:
            _log.debug("clear_buttons: %s", exc)
