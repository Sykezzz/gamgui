"""Pure ASGI request-body limits applied before multipart parsing."""

from __future__ import annotations

from typing import Awaitable, Callable

from starlette.responses import HTMLResponse


class _RequestBodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    """Reject an oversized body while Starlette is still consuming ASGI chunks."""

    def __init__(
        self,
        app,
        *,
        path: str,
        maximum_bytes: int,
    ) -> None:
        self.app = app
        self.path = str(path)
        self.maximum_bytes = max(1, int(maximum_bytes))

    async def __call__(self, scope, receive, send) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method", "")).upper() != "POST"
            or str(scope.get("path", "")) != self.path
        ):
            await self.app(scope, receive, send)
            return

        declared = _content_length(scope)
        if declared is not None and declared > self.maximum_bytes:
            await self._reject(scope, receive, send)
            return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.maximum_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope, receive, send) -> None:
        response = HTMLResponse(
            (
                '<div role="alert"><p>The upload is too large.</p>'
                '<p>Error code: <code>OR-ZIP-SIZE</code></p></div>'
            ),
            status_code=413,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
        await response(scope, receive, send)


def _content_length(scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", ()):
        if bytes(raw_name).lower() != b"content-length":
            continue
        try:
            value = int(bytes(raw_value).decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return None
        return max(0, value)
    return None
