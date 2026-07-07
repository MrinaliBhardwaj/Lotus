from httpx import AsyncClient


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app"] == "lexa"


async def test_health_carries_request_id(client: AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "req-abc"})
    assert response.headers["X-Request-ID"] == "req-abc"


async def test_minted_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers.get("X-Request-ID")


async def test_oversized_body_rejected(client: AsyncClient) -> None:
    response = await client.post(
        "/health", headers={"Content-Length": str(100 * 1024 * 1024 * 1024)}
    )
    assert response.status_code == 413
