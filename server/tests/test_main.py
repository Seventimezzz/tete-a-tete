import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_http_docs_are_not_served(path: str) -> None:
    assert TestClient(app).get(path).status_code == 404


async def test_app_answers_over_asgi_in_async_test() -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 404
