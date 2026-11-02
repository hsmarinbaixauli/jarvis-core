"""Keeps `python -m jarvis run` alive: restarts it after a crash, gives up if it keeps crashing.

Exit code 0 means Jarvis stopped on purpose (Ctrl+C, shutdown), so the supervisor stops too.
"""

from __future__ import annotations

import logging
import subprocess
import time
from collections import deque
from collections.abc import Callable, Sequence

_log = logging.getLogger(__name__)

BACKOFF_S: tuple[int, ...] = (5, 15, 60, 300)


def supervise(
    cmd: Sequence[str],
    *,
    max_restarts: int = 5,
    window_s: float = 3600,
    run: Callable[[Sequence[str]], int] = subprocess.call,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    on_give_up: Callable[[str], None] | None = None,
) -> int:
    crashes: deque[float] = deque()
    while True:
        code = run(list(cmd))
        if code == 0:
            _log.info("Jarvis se ha detenido normalmente.")
            return 0
        now = clock()
        crashes.append(now)
        while crashes and now - crashes[0] > window_s:
            crashes.popleft()
        if len(crashes) > max_restarts:
            message = (
                f"Jarvis se ha caído {len(crashes)} veces en {int(window_s // 60)} min (código {code}); "
                "no lo reinicio más. Revisa data/logs/jarvis.log."
            )
            _log.error(message)
            if on_give_up is not None:
                try:
                    on_give_up(message)
                except Exception:
                    _log.exception("Could not report the give-up")
            return code
        delay = BACKOFF_S[min(len(crashes) - 1, len(BACKOFF_S) - 1)]
        _log.warning("Jarvis terminó con código %s; lo reinicio en %d s.", code, delay)
        sleep(delay)


def telegram_alert(token: str, chat_id: int) -> Callable[[str], None]:
    """Last-resort alert that works even if Jarvis itself cannot start."""
    import httpx

    def send(text: str) -> None:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🔴 {text}"},
            timeout=15,
        )

    return send
