"""Voice through a local Voicebox (github.com/jamiepine/voicebox): speech-to-text and text-to-speech.

Only the Telegram channel uses this, over Voicebox's REST API. The brain never gets Voicebox's own MCP
tools: a voice note becomes plain text for the core, and a reply becomes a voice note. Audio never leaves
the PC: the URL must point at this machine (see config.py).
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx

_log = logging.getLogger(__name__)

# Opus only takes these sample rates; anything else is resampled to 48 kHz.
_OPUS_RATES = (8000, 12000, 16000, 24000, 48000)
_URL = re.compile(r"https?://\S+")
_MARKUP = re.compile(r"[*_`#>|~]+")


class VoiceError(RuntimeError):
    pass


class VoiceBusy(VoiceError):
    """Voicebox is downloading a model: worth trying again in a few minutes."""


@dataclass(frozen=True)
class VoiceSettings:
    url: str
    profile: str
    language: str = "es"
    whisper_model: str = "turbo"
    engine: str | None = None
    model_size: str | None = None
    reply_with_voice: bool = True
    max_seconds: int = 300
    max_reply_chars: int = 1500


def speakable(text: str) -> str:
    """Reply text as it should be read aloud: no Markdown marks, links named instead of spelled out."""
    text = _URL.sub("(enlace)", text)
    text = _MARKUP.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def to_voice_note(wav: bytes) -> bytes:
    """WAV → OGG/Opus, the only format Telegram shows as a voice note."""
    import numpy as np
    import soundfile as sf

    try:
        audio, rate = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    except (RuntimeError, sf.LibsndfileError) as exc:
        raise VoiceError(f"Voicebox no devolvió un audio válido: {exc}") from exc
    mono = audio.mean(axis=1)
    if rate not in _OPUS_RATES:
        target = 48000
        positions = np.linspace(0, len(mono) - 1, num=max(1, round(len(mono) * target / rate)))
        mono = np.interp(positions, np.arange(len(mono)), mono).astype("float32")
        rate = target
    out = io.BytesIO()
    sf.write(out, mono, rate, format="OGG", subtype="OPUS")
    return out.getvalue()


class VoiceboxClient:
    def __init__(self, settings: VoiceSettings, http: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._base = settings.url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=5.0))
        self._download_requested = False

    async def close(self) -> None:
        await self._http.aclose()

    async def health(self) -> dict[str, Any]:
        resp = await self._http.get(f"{self._base}/health")
        _check(resp, "health")
        return resp.json()

    async def transcribe(self, audio: bytes, filename: str = "nota.ogg") -> str:
        resp = await self._http.post(
            f"{self._base}/transcribe",
            files={"file": (filename, audio, "application/octet-stream")},
            data={"language": self.settings.language, "model": self.settings.whisper_model},
        )
        if resp.status_code == 202:
            raise VoiceBusy("Estoy descargando el modelo para entender la voz; prueba otra vez en un par de minutos.")
        _check(resp, "transcribe")
        return str(resp.json().get("text", "")).strip()

    async def find_profile(self) -> dict[str, Any]:
        resp = await self._http.get(f"{self._base}/profiles")
        _check(resp, "profiles")
        wanted = self.settings.profile.strip().lower()
        for profile in resp.json():
            if wanted in (str(profile.get("id", "")).lower(), str(profile.get("name", "")).lower()):
                return profile
        raise VoiceError(f"No existe el perfil de voz «{self.settings.profile}» en Voicebox (voice.profile).")

    async def speak(self, text: str) -> bytes:
        """Synthesise *text* with the configured profile and return it as an OGG/Opus voice note."""
        profile = await self.find_profile()
        engine = self.settings.engine or profile.get("default_engine") or profile.get("preset_engine") or "qwen"
        body: dict[str, Any] = {
            "profile_id": profile["id"],
            "text": text,
            "language": self.settings.language,
            "engine": engine,
        }
        if self.settings.model_size:
            body["model_size"] = self.settings.model_size
        resp = await self._http.post(f"{self._base}/generate/stream", json=body)
        if resp.status_code == 400 and "not downloaded" in resp.text:
            # /generate (not the stream) starts the model download in Voicebox's background.
            if not self._download_requested:
                self._download_requested = True
                await self._http.post(f"{self._base}/generate", json=body)
            raise VoiceBusy("Estoy descargando el modelo de voz; en unos minutos te contesto también con voz.")
        _check(resp, "generate")
        return to_voice_note(resp.content)


def _check(resp: httpx.Response, what: str) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise VoiceError(f"Voicebox ({what}): HTTP {resp.status_code} {str(detail)[:200]}")
