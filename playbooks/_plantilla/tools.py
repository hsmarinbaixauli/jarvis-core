"""Plantilla: herramientas de un playbook. Cada herramienta declara su efecto en SPECS."""

from __future__ import annotations

from typing import Any

from callgate import Effect, ToolSpec

from jarvis.playbooks import PlaybookContext, PlaybookTools, json_text

# Efecto: READ (lo hace sin avisar) · WRITE (lo hace y te avisa) · IRREVERSIBLE (espera siempre tu ✅).
# tier=Tier.FREE / Tier.APPROVAL ajusta el nivel; lo irreversible nunca baja de aprobación.
SPECS: tuple[ToolSpec, ...] = (ToolSpec("example_hello", Effect.READ, "Saludo de ejemplo.", external=False),)


def build(ctx: PlaybookContext) -> PlaybookTools:
    from claude_agent_sdk import tool

    @tool("example_hello", "Herramienta de ejemplo: devuelve un saludo.", {"name": str})
    async def example_hello(args: dict[str, Any]) -> dict[str, Any]:
        return json_text(f"Hola, {args.get('name', 'mundo')}. Ajuste de ejemplo: {ctx.settings.get('ejemplo')}")

    return PlaybookTools(tools=[example_hello], specs=SPECS)
