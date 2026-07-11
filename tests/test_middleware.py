"""Body-size-cap middleware: the cap must hold on streamed bytes, not just on
the Content-Length header (C3 — a header-only check is bypassable with a
chunked body and would let an unauthenticated caller buffer unbounded memory).
"""

from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.core.middleware import BodySizeLimitMiddleware

CAP = 1024


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=CAP, exempt_prefixes=("/raw/",))

    @app.post("/echo")
    async def echo(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"len": len(body)}

    @app.post("/raw/{key}")
    async def raw(request: Request) -> dict[str, int]:
        body = await request.body()
        return {"len": len(body)}

    return app


async def _client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test")


async def test_within_cap_passes() -> None:
    async with await _client() as client:
        response = await client.post("/echo", content=b"x" * 512)
        assert response.status_code == 200
        assert response.json() == {"len": 512}


async def test_declared_length_over_cap_rejected() -> None:
    async with await _client() as client:
        response = await client.post("/echo", content=b"x" * (CAP + 1))
        assert response.status_code == 413
        assert response.json()["error"] == "RequestTooLarge"


async def test_chunked_body_over_cap_rejected() -> None:
    # an async-iterable body → httpx uses Transfer-Encoding: chunked, so there
    # is NO Content-Length header. The header-only check would wave this
    # through; the streaming tally must catch it.
    async def stream() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 512  # 2 KiB total, no declared length

    async with await _client() as client:
        response = await client.post("/echo", content=stream())
        assert response.status_code == 413
        assert response.json()["error"] == "RequestTooLarge"


async def test_chunked_body_within_cap_passes() -> None:
    async def stream() -> AsyncIterator[bytes]:
        yield b"x" * 256
        yield b"x" * 256

    async with await _client() as client:
        response = await client.post("/echo", content=stream())
        assert response.status_code == 200
        assert response.json() == {"len": 512}


async def test_exempt_prefix_is_not_capped() -> None:
    async def stream() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b"x" * 512  # 2 KiB, over the cap, but exempt

    async with await _client() as client:
        response = await client.post("/raw/key", content=stream())
        assert response.status_code == 200
        assert response.json() == {"len": 2048}


async def test_invalid_content_length_rejected() -> None:
    async with await _client() as client:
        response = await client.post(
            "/echo", content=b"x", headers={"content-length": "not-a-number"}
        )
        assert response.status_code == 400
