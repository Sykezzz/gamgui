from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from gamgui.web.limits import RequestBodyLimitMiddleware


def _app(maximum: int = 32) -> FastAPI:
    app = FastAPI()
    app.add_middleware(
        RequestBodyLimitMiddleware,
        path="/classroom/imports/upload",
        maximum_bytes=maximum,
    )

    @app.post("/classroom/imports/upload")
    async def upload(request: Request):
        return PlainTextResponse(str(len(await request.body())))

    @app.post("/other")
    async def other(request: Request):
        return PlainTextResponse(str(len(await request.body())))

    return app


def test_declared_oversized_upload_is_rejected_before_endpoint() -> None:
    response = TestClient(_app()).post(
        "/classroom/imports/upload",
        content=b"x" * 33,
    )

    assert response.status_code == 413
    assert "OR-ZIP-SIZE" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_exact_limit_and_other_routes_are_unchanged() -> None:
    client = TestClient(_app())

    assert client.post(
        "/classroom/imports/upload", content=b"x" * 32
    ).text == "32"
    assert client.post("/other", content=b"x" * 100).text == "100"


def test_chunked_body_is_counted_without_content_length() -> None:
    middleware = RequestBodyLimitMiddleware(
        lambda scope, receive, send: _consume(scope, receive, send),
        path="/classroom/imports/upload",
        maximum_bytes=5,
    )
    messages = iter(
        (
            {"type": "http.request", "body": b"abc", "more_body": True},
            {"type": "http.request", "body": b"def", "more_body": False},
        )
    )
    sent: list[dict] = []

    async def receive():
        return next(messages)

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(
        middleware(
            {
                "type": "http",
                "method": "POST",
                "path": "/classroom/imports/upload",
                "headers": (),
            },
            receive,
            send,
        )
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413


async def _consume(scope, receive, send) -> None:
    while True:
        message = await receive()
        if not message.get("more_body", False):
            break
