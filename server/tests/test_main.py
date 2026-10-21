import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_http_docs_are_not_served(path: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
