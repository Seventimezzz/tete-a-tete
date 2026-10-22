import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

SERVER_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_http_docs_are_not_served(path: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404


def test_import_installs_log_filter_without_secret() -> None:
    code = (
        "import logging\n"
        "import app.main\n"
        "from app.logs import PrivacyFilter\n"
        "targets = [logging.getLogger(n) for n in ('uvicorn', 'uvicorn.error', 'websockets')]\n"
        "targets.append(logging.lastResort)\n"
        "print(all(any(isinstance(f, PrivacyFilter) for f in t.filters) for t in targets))\n"
    )
    env = {"PATH": "", "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
