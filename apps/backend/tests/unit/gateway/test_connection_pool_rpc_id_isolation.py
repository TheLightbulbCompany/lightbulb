"""GW-3: two callers using the SAME client req_id must not cross responses.

The client-supplied ``req_id`` is echoed back to that client verbatim but is not
unique across the tabs / org-members that share one per-owner GatewayConnection.
The pending-futures map is per-owner, so a duplicate id silently overwrote one
caller's future and crossed their responses. ``pool.send_rpc`` now mints a fresh
wire id per call, so the on-the-wire id (hence the ``_pending_rpcs`` key) can
never collide even when both callers pass the identical client id.
"""

import asyncio
import json

import pytest

from core.gateway.connection_pool import GatewayConnection, GatewayConnectionPool


class _FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


def _bare_pool() -> GatewayConnectionPool:
    return GatewayConnectionPool.__new__(GatewayConnectionPool)


@pytest.mark.asyncio
async def test_same_client_req_id_does_not_cross_responses():
    # One shared per-owner connection with the REAL send_rpc / wait_for_response
    # / _handle_message path.
    conn = GatewayConnection.__new__(GatewayConnection)
    conn._ws = _FakeWS()
    conn._pending_rpcs = {}

    pool = _bare_pool()

    async def fake_ensure(user_id, ip, token):
        return conn

    pool.ensure_connection = fake_ensure

    # Two callers, IDENTICAL client-chosen req_id.
    task_a = asyncio.create_task(pool.send_rpc("owner", "dup", "m", {}, "ip", "tok"))
    task_b = asyncio.create_task(pool.send_rpc("owner", "dup", "m", {}, "ip", "tok"))

    # Let both send their frame + register their future.
    for _ in range(4):
        await asyncio.sleep(0)

    wire_ids = [frame["id"] for frame in conn._ws.sent]
    assert len(wire_ids) == 2
    # Despite the identical client id, the two wire ids are distinct — so the
    # two futures live under distinct keys and cannot overwrite each other.
    assert len(set(wire_ids)) == 2
    assert len(conn._pending_rpcs) == 2

    # Deliver a DISTINCT response for each wire id.
    await conn._handle_message({"type": "res", "id": wire_ids[0], "ok": True, "payload": {"tag": "first"}})
    await conn._handle_message({"type": "res", "id": wire_ids[1], "ok": True, "payload": {"tag": "second"}})

    res_a = await asyncio.wait_for(task_a, timeout=1)
    res_b = await asyncio.wait_for(task_b, timeout=1)

    # Both callers completed (neither was orphaned by an overwrite) and each got
    # a real, distinct payload — no cross, no timeout.
    assert {res_a["tag"], res_b["tag"]} == {"first", "second"}
