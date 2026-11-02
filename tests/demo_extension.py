"""A minimal extension used by the tests: one declared tool and one Telegram command."""

from __future__ import annotations

from callgate import Effect, ToolRegistry, ToolSpec

from jarvis.extensions import ExtensionContext

NAME = "demo"


class DemoExtension:
    name = NAME

    def declare(self, registry: ToolRegistry) -> None:
        registry.add_server(NAME, [ToolSpec("demo_ping", Effect.WRITE, "Ping de prueba.", external=False)])

    def setup(self, ctx: ExtensionContext) -> None:
        async def ping(args: str, channel: str) -> str:
            return "pong"

        ctx.core.register_command("ping", "Responde pong.", ping)


def create() -> DemoExtension:
    return DemoExtension()
