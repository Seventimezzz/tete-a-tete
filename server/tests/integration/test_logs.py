"""The server output holds no personal data or access keys (pinning item 1.1)."""

import secrets

import pytest
from websockets.exceptions import ConnectionClosed, InvalidStatus

from app.protocol import new_room_id
from tests.integration.conftest import StartServer
from tests.integration.factory import FAULT_NAME
from tests.integration.harness import NET, Client, RunningServer

pytestmark = pytest.mark.integration


async def test_query_string_is_not_logged(server: RunningServer) -> None:
    owner = await Client.connect(server)
    room_id = (await owner.create("Хозяин-запрос"))["roomId"]
    guest = await Client.connect(server, path=f"/ws?r={room_id}")
    await guest.join(room_id, "Гость-запрос")
    # A refused handshake is logged by uvicorn too, with its path and query.
    with pytest.raises(InvalidStatus):
        await Client.connect(server, path=f"/nowhere?r={room_id}")
    for client in (owner, guest):
        await client.close()


async def test_injected_error_logs_type_name_only(start: StartServer) -> None:
    server = start(env={"TETE_TEST_FAULT": "1"})
    room_id = new_room_id()
    token = f"tokenmarker.{secrets.token_urlsafe(16)}"
    client = await Client.connect(server)
    await client.send_json(
        {"type": "join", "roomId": room_id, "name": FAULT_NAME, "net": NET, "resumeToken": token}
    )
    with pytest.raises(ConnectionClosed):
        await client.recv_json()
    probe = await Client.connect(server)
    await probe.ping()
    await probe.close()

    output = server.stop()
    assert "ValueError" in output
    assert {FAULT_NAME, room_id, token} <= server.sensitive  # checked by the fixture
