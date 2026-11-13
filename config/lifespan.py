"""ASGI lifespan handling for one metadata refresh per worker."""

from __future__ import annotations

from typing import Any


class CatalogLifespan:
    def __init__(self, application: Any) -> None:
        self.application = application
        self.started = False

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "lifespan":
            await self.application(scope, receive, send)
            return
        while True:
            event = await receive()
            if event["type"] == "lifespan.startup":
                if not self.started:
                    self.started = True
                    from datasets.services import refresh_catalog_on_startup

                    await refresh_catalog_on_startup()
                await send({"type": "lifespan.startup.complete"})
            elif event["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
