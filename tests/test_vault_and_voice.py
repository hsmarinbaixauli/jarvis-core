from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from callgate import PolicyContext, Tier, classify, is_external

from jarvis.config import ConfigError, load_settings
from jarvis.security import core_registry
from jarvis.telegram.bot import TelegramBot
from jarvis.vault import Vault, VaultError
from jarvis.voice import VoiceboxClient, VoiceBusy, VoiceError, VoiceSettings, speakable, to_voice_note

OWNER = 4242


def _config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "jarvis.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# -- vault -------------------------------------------------------------------------------------


@pytest.fixture()
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "vault")
    v.ensure()
    (v.root / "Ideas.md").write_text("# Ideas\nUna landing con scroll para Acme.\n", encoding="utf-8")
    (v.root / ".obsidian").mkdir()
    (v.root / ".obsidian" / "secreto.md").write_text("acme", encoding="utf-8")
    return v


@pytest.mark.parametrize("bad", ["../fuera", "/etc/passwd", "C:/Windows/win", ".obsidian/app", "a/../../b", ""])
def test_vault_paths_stay_inside(vault: Vault, bad: str) -> None:
    with pytest.raises(VaultError):
        vault.resolve(bad)


def test_vault_resolve_adds_md(vault: Vault) -> None:
    assert vault.relative(vault.resolve("Jarvis/Diario")) == "Jarvis/Diario.md"
    assert vault.relative(vault.resolve("Jarvis\\Diario.md")) == "Jarvis/Diario.md"


def test_jarvis_writes_only_under_its_folder(vault: Vault) -> None:
    assert vault.create("Empresas/Acme", "# Acme\n[[Ideas]]") == "Jarvis/Empresas/Acme.md"
    assert vault.create("Jarvis/Diario", "Día 1") == "Jarvis/Diario.md"
    with pytest.raises(VaultError, match="Ya existe"):
        vault.create("Jarvis/Diario", "otra vez")
    with pytest.raises(VaultError, match=r"'\.\.'"):
        vault.create("../Ideas", "pisar la nota del dueño")
    (vault.root / "Jarvis.md").write_text("Nota del dueño sobre Jarvis\n", encoding="utf-8")
    for name in ("Jarvis", "Jarvis.md", "Jarvis/"):
        with pytest.raises(VaultError):
            vault.append(name, "colado")
    assert "colado" not in (vault.root / "Jarvis.md").read_text(encoding="utf-8")
    vault.append("Diario", "Día 2")
    assert "Día 1" in vault.read("Jarvis/Diario") and "Día 2" in vault.read("Jarvis/Diario")
    with pytest.raises(VaultError, match="No existe"):
        vault.append("Ideas", "nota del dueño")  # becomes Jarvis/Ideas, which does not exist
    assert "Día" not in (vault.root / "Ideas.md").read_text(encoding="utf-8")


def test_vault_edit_reaches_owner_notes(vault: Vault) -> None:
    assert vault.edit("Ideas", "# Ideas\nNueva") == "Ideas.md"
    assert vault.read("Ideas").endswith("Nueva\n")


def test_vault_search_and_list_skip_hidden(vault: Vault) -> None:
    vault.create("Empresas/Acme", "Marca de zapatillas; quieren landing.")
    hits = vault.search("acme landing")
    assert [h["note"] for h in hits] == ["Jarvis/Empresas/Acme.md", "Ideas.md"]
    assert all(".obsidian" not in vault.relative(p) for p in vault.notes())
    assert [vault.relative(p) for p in vault.notes("Jarvis")] == ["Jarvis/Empresas/Acme.md"]
    with pytest.raises(VaultError):
        vault.search("   ")


def test_vault_tiers() -> None:
    ctx = PolicyContext(registry=core_registry(), allowed_dirs=(Path("."),))
    assert classify("mcp__vault__vault_search", {}, ctx).tier is Tier.FREE
    assert classify("mcp__vault__vault_create", {}, ctx).tier is Tier.NOTIFY
    assert classify("mcp__vault__vault_append", {}, ctx).tier is Tier.NOTIFY
    assert classify("mcp__vault__vault_edit", {}, ctx).tier is Tier.APPROVAL
    assert is_external("mcp__vault__vault_read", ctx)
    assert not is_external("mcp__vault__vault_create", ctx)


def test_vault_tools_report_errors(vault: Vault) -> None:
    pytest.importorskip("claude_agent_sdk")
    from jarvis.vault import build_tools

    tools = {t.name: t for t in build_tools(vault)}

    def call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(tools[name].handler(args))

    assert "Creada Jarvis/Nota.md" in call("vault_create", {"note": "Nota", "content": "hola"})["content"][0]["text"]
    assert call("vault_read", {"note": "../../etc/passwd"}).get("is_error")
    assert call("vault_create", {"note": "Vacía", "content": "  "}).get("is_error")
    assert "Jarvis/Nota.md" in call("vault_list", {"folder": "Jarvis"})["content"][0]["text"]


def test_vault_config(tmp_path: Path) -> None:
    settings = load_settings(_config(tmp_path, "vault:\n  path: boveda\n"))
    assert settings.vault_dir == (tmp_path / "boveda").resolve()
    assert settings.vault_dir in settings.protected_paths
    assert load_settings(_config(tmp_path, "{}\n")).vault_dir is None
    assert load_settings(_config(tmp_path, "vault:\n  enabled: false\n  path: boveda\n")).vault_dir is None
    with pytest.raises(ConfigError, match=r"vault\.path"):
        load_settings(_config(tmp_path, "vault:\n  enabled: true\n"))


# -- voice config and helpers -------------------------------------------------------------------


def test_voice_config(tmp_path: Path) -> None:
    assert load_settings(_config(tmp_path, "{}\n")).voice is None
    voice = load_settings(_config(tmp_path, "voice:\n  profile: Jarvis\n")).voice
    assert voice is not None and voice.url == "http://127.0.0.1:17493" and voice.whisper_model == "turbo"
    docker = load_settings(_config(tmp_path, "voice:\n  url: http://host.docker.internal:17493\n  profile: J\n"))
    assert docker.voice is not None
    assert load_settings(_config(tmp_path, "voice:\n  enabled: false\n")).voice is None


@pytest.mark.parametrize(
    "body",
    [
        "voice:\n  url: http://192.168.1.20:17493\n  profile: J\n",
        "voice:\n  url: https://voz.example.com\n  profile: J\n",
        "voice:\n  url: http://127.0.0.1.evil.com:17493\n  profile: J\n",
        "voice:\n  url: http://127.0.0.1:17493/x\n  profile: J\n",
        "voice:\n  enabled: true\n",
        "voice:\n  profile: J\n  whisper_model: enorme\n",
    ],
)
def test_voice_config_rejects(tmp_path: Path, body: str) -> None:
    with pytest.raises(ConfigError):
        load_settings(_config(tmp_path, body))


def test_speakable() -> None:
    assert speakable("**Hecho**: mira https://x.com/a?b=1 y `código`") == "Hecho: mira (enlace) y código"


def _wav(rate: int, seconds: float = 0.2) -> bytes:
    np = pytest.importorskip("numpy")
    sf = pytest.importorskip("soundfile")
    out = io.BytesIO()
    samples = (0.2 * np.sin(np.linspace(0, 440 * seconds * 6.283, int(rate * seconds)))).astype("float32")
    sf.write(out, np.stack([samples, samples], axis=1), rate, format="WAV")
    return out.getvalue()


@pytest.mark.parametrize("rate", [24000, 22050])
def test_to_voice_note_is_ogg_opus(rate: int) -> None:
    sf = pytest.importorskip("soundfile")
    ogg = to_voice_note(_wav(rate))
    info = sf.info(io.BytesIO(ogg))
    assert ogg.startswith(b"OggS") and info.channels == 1 and info.samplerate in (24000, 48000)
    with pytest.raises(VoiceError):
        to_voice_note(b"no es audio")


# -- Voicebox client ------------------------------------------------------------------------------


SETTINGS = VoiceSettings(url="http://127.0.0.1:17493", profile="jarvis")
PROFILES = [{"id": "p1", "name": "Otra"}, {"id": "p2", "name": "Jarvis", "preset_engine": "kokoro"}]


def _client(handler: Any) -> VoiceboxClient:
    return VoiceboxClient(SETTINGS, httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def test_transcribe_sends_language_and_model() -> None:
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transcribe"
        seen.append(request.content)
        return httpx.Response(200, json={"text": " Hola Jarvis ", "duration": 1.0})

    assert asyncio.run(_client(handler).transcribe(b"OggS...")) == "Hola Jarvis"
    assert b'name="language"\r\n\r\nes' in seen[0] and b'name="model"\r\n\r\nturbo' in seen[0]


def test_transcribe_while_whisper_downloads() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(202, json={"detail": {"downloading": True}})

    with pytest.raises(VoiceBusy):
        asyncio.run(_client(handler).transcribe(b"x"))


def test_speak_uses_profile_engine_and_converts() -> None:
    bodies: list[dict[str, Any]] = []
    wav = _wav(24000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/profiles":
            return httpx.Response(200, json=PROFILES)
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=wav, headers={"content-type": "audio/wav"})

    ogg = asyncio.run(_client(handler).speak("Hola"))
    assert ogg.startswith(b"OggS")
    assert bodies == [{"profile_id": "p2", "text": "Hola", "language": "es", "engine": "kokoro"}]


def test_speak_triggers_model_download_once() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/profiles":
            return httpx.Response(200, json=PROFILES)
        if request.url.path == "/generate/stream":
            return httpx.Response(400, json={"detail": "Kokoro model is not downloaded yet."})
        return httpx.Response(200, json={"id": "g1", "status": "downloading"})

    client = _client(handler)
    for _ in range(2):
        with pytest.raises(VoiceBusy):
            asyncio.run(client.speak("Hola"))
    assert paths.count("/generate") == 1


def test_unknown_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "p1", "name": "Otra"}])

    with pytest.raises(VoiceError, match="perfil"):
        asyncio.run(_client(handler).speak("Hola"))


# -- Telegram voice notes ---------------------------------------------------------------------------


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.voices: list[bytes] = []
        self.downloads: list[str] = []

    async def send_message(self, chat_id: int, text: str, buttons: Any = None) -> None:
        self.sent.append(text)

    async def send_typing(self, chat_id: int) -> None:
        pass

    async def download_file(self, file_id: str) -> bytes:
        self.downloads.append(file_id)
        return b"OggS-nota"

    async def send_voice(self, chat_id: int, ogg: bytes, caption: str = "") -> None:
        self.voices.append(ogg)


class FakeVoice:
    def __init__(self, heard: str = "qué tengo mañana", reply_with_voice: bool = True) -> None:
        self.settings = VoiceSettings(url="http://127.0.0.1:17493", profile="J", reply_with_voice=reply_with_voice)
        self.heard = heard
        self.spoken: list[str] = []

    async def transcribe(self, audio: bytes, filename: str = "nota.ogg") -> str:
        return self.heard

    async def speak(self, text: str) -> bytes:
        self.spoken.append(text)
        return b"OggS-respuesta"


def _voice_msg(chat: int, duration: int = 3) -> dict[str, Any]:
    return {"update_id": 1, "message": {"chat": {"id": chat}, "voice": {"file_id": "f1", "duration": duration}}}


def _bot(core: Any, voice: Any) -> tuple[TelegramBot, FakeTelegram]:
    client = FakeTelegram()
    bot = TelegramBot(client, core, OWNER, voice)  # type: ignore[arg-type]
    core.register(bot)
    return bot, client


def test_voice_note_round_trip(core: Any, brain_script: Any) -> None:
    voice = FakeVoice()
    bot, client = _bot(core, voice)

    async def scenario() -> None:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            await bot.handle_update(_voice_msg(OWNER))
            while not client.voices:
                await asyncio.sleep(0.01)
        finally:
            dispatcher.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert client.sent[0] == "🎙️ «qué tengo mañana»"
    assert "eco: qué tengo mañana" in client.sent
    assert brain_script.calls[0]["text"] == "qué tengo mañana"
    assert voice.spoken == ["eco: qué tengo mañana"] and client.voices == [b"OggS-respuesta"]


def test_typed_messages_are_not_spoken(core: Any) -> None:
    voice = FakeVoice()
    bot, client = _bot(core, voice)

    async def scenario() -> None:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            await bot.handle_update({"update_id": 1, "message": {"chat": {"id": OWNER}, "text": "hola"}})
            while "eco: hola" not in client.sent:
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        finally:
            dispatcher.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert voice.spoken == [] and client.voices == []


def test_voice_reply_can_be_turned_off(core: Any) -> None:
    voice = FakeVoice(reply_with_voice=False)
    bot, client = _bot(core, voice)

    async def scenario() -> None:
        dispatcher = asyncio.create_task(core.queue.run_forever())
        try:
            await bot.handle_update(_voice_msg(OWNER))
            while not any(t.startswith("eco:") for t in client.sent):
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)
        finally:
            dispatcher.cancel()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert client.voices == []


def test_transcribed_slash_is_not_a_command(core: Any, store: Any) -> None:
    bot, _ = _bot(core, FakeVoice(heard="/parar"))
    asyncio.run(bot.handle_update(_voice_msg(OWNER)))
    assert not core.halted and store.list_tasks()[0]["prompt"] == "/parar"


def test_stranger_voice_notes_are_not_downloaded(core: Any, store: Any) -> None:
    bot, client = _bot(core, FakeVoice())
    asyncio.run(bot.handle_update(_voice_msg(999)))
    assert client.downloads == [] and client.sent == [] and store.list_tasks() == []


def test_voice_note_without_voicebox(core: Any, store: Any) -> None:
    bot, client = _bot(core, None)
    asyncio.run(bot.handle_update(_voice_msg(OWNER)))
    assert "No tengo la voz activada" in client.sent[0] and store.list_tasks() == []


def test_too_long_voice_note(core: Any, store: Any) -> None:
    bot, client = _bot(core, FakeVoice())
    asyncio.run(bot.handle_update(_voice_msg(OWNER, duration=900)))
    assert "más de 300 s" in client.sent[0] and client.downloads == [] and store.list_tasks() == []


def test_voicebox_down_is_reported(core: Any, store: Any) -> None:
    class DownVoice(FakeVoice):
        async def transcribe(self, audio: bytes, filename: str = "nota.ogg") -> str:
            raise httpx.ConnectError("refused")

    bot, client = _bot(core, DownVoice())
    asyncio.run(bot.handle_update(_voice_msg(OWNER)))
    assert "Voicebox" in client.sent[0] and store.list_tasks() == []
