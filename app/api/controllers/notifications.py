"""Notification streaming endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import Any, TypeVar, cast

from fastapi import HTTPException, status
from fastapi_controller import controller
from fastapi.responses import StreamingResponse

from app.api.controllers.base import BaseController
from app.api.deps.auth import CurrentUser
from app.services.notifications import NotificationEvent, get_notification_hub

F = TypeVar("F", bound=Callable[..., Any])


def typed_get(path: str) -> Callable[[F], F]:
    """Typed wrapper for controller.get."""
    return cast(Callable[[F], F], controller.get(path))


class NotificationController(BaseController):  # type: ignore[misc]
    """Real-time notification endpoints."""

    @typed_get("/stream")
    async def stream_notifications(self, current_user: CurrentUser) -> StreamingResponse:
        """Stream order and delivery notifications over server-sent events."""
        if current_user.id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

        hub = get_notification_hub()
        queue = hub.subscribe(current_user.id)

        async def events() -> AsyncGenerator[str, None]:
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        event: NotificationEvent = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield ": keep-alive\n\n"
                        continue
                    yield f"event: {event.type}\ndata: {event.model_dump_json()}\n\n"
            finally:
                hub.unsubscribe(current_user.id, queue)

        return StreamingResponse(events(), media_type="text/event-stream")
