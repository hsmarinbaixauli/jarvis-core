"""Weather phrase formatter for Jarvis greetings."""

from __future__ import annotations

from typing import Any

_WET_KEYWORDS: frozenset[str] = frozenset(["lluvia", "llovizna", "chubasco", "tormenta", "nieve"])


def format_weather_for_greeting(weather: dict[str, Any]) -> str:
    """Return a natural-language Spanish TTS phrase for the startup greeting.

    Examples:
        "Hace 22 grados y soleado."
        "Hace 8 grados y llueve. Lleva paraguas."
    """
    temp: int | str = weather.get("temperature", "?")
    description: str = weather.get("description", "")
    phrase: str = f"Hace {temp} grados y {description}."
    if any(kw in description.lower() for kw in _WET_KEYWORDS):
        phrase += " Lleva paraguas."
    return phrase
