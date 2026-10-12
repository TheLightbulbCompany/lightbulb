import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services import node_broker


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_the_challenge_is_an_event_with_a_nonce_and_a_non_negative_integer_ts():
    frame = node_broker.challenge_frame()
    assert frame["type"] == "event" and frame["event"] == "connect.challenge"
    assert isinstance(frame["payload"]["nonce"], str) and frame["payload"]["nonce"].strip()
    assert isinstance(frame["payload"]["ts"], int) and frame["payload"]["ts"] >= 0


def test_hello_ok_matches_the_upstream_clients_checklist_field_for_field():
    hello = node_broker.hello_ok("req-1", "conn-1")
    assert hello["type"] == "res" and hello["id"] == "req-1" and hello["ok"] is True
    payload = hello["payload"]
    assert payload["type"] == "hello-ok" and payload["protocol"] == 4
    assert payload["server"]["version"] and payload["server"]["connId"] == "conn-1"
    assert isinstance(payload["features"]["methods"], list) and isinstance(payload["features"]["events"], list)
    assert payload["snapshot"] == {
        "presence": [],
        "health": {},
        "stateVersion": {"presence": 0, "health": 0},
        "uptimeMs": 0,
    }
    assert payload["auth"] == {"role": "node", "scopes": []}
    assert payload["policy"] == {"maxPayload": 26214400, "maxBufferedBytes": 52428800, "tickIntervalMs": 15000}


def test_hello_ok_never_mints_a_device_token():
    # Present, the CLI persists it and every later reconnect authenticates with OUR token
    # instead of the device identity — the Keychain path goes cold (client.ts:951-963).
    assert "deviceToken" not in node_broker.hello_ok("r", "c")["payload"]["auth"]


def test_the_v3_payload_string_is_pipe_joined_in_the_locked_order():
    params = {
        "client": {"id": "openclaw-macos", "mode": "node", "platform": "macOS"},
        "role": "node",
        "scopes": [],
        "auth": {"token": "gw-tok"},
        "device": {"id": "did-abc", "signedAt": 1700000000000, "nonce": "nonce-xyz"},
    }
    assert node_broker.device_payload_v3(params, "nonce-xyz") == (
        "v3|did-abc|openclaw-macos|node|node||1700000000000|gw-tok|nonce-xyz|macos|"
    )


def test_device_family_is_read_from_client_where_the_cli_node_host_puts_it():
    # The vendored CLI's buildDeviceAuthPayloadV3 signs `client.deviceFamily` ("mac"); the
    # Rust device client sends none. Reading it off `device` produced "...|macos|" for a
    # frame signed as "...|macos|mac", so every real CLI connect failed bad_signature
    # (found by the Task 30 loopback proof, 2026-09-17).
    params = {
        "client": {"id": "node-host", "mode": "node", "platform": "macOS", "deviceFamily": "Mac"},
        "role": "node",
        "scopes": [],
        "auth": {"token": "gw-tok"},
        "device": {"id": "did-abc", "signedAt": 1700000000000, "nonce": "nonce-xyz"},
    }
    assert node_broker.device_payload_v3(params, "nonce-xyz").endswith("|macos|mac")


def test_a_real_ed25519_signature_verifies_and_a_tampered_one_does_not():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    params = {
        "client": {"id": "node-host", "mode": "node", "platform": "macos"},
        "role": "node",
        "scopes": [],
        "auth": {"token": "t"},
        "device": {"id": "d", "signedAt": 1, "nonce": "n", "publicKey": _b64u(raw)},
    }
    params["device"]["signature"] = _b64u(key.sign(node_broker.device_payload_v3(params, "n").encode()))
    assert node_broker.signature_matches(params, "n") is True
    params["device"]["signature"] = _b64u(b"\x00" * 64)
    assert node_broker.signature_matches(params, "n") is False


def test_a_clock_skewed_signed_at_is_not_a_rejection():
    # Our Rust client signs with its LOCAL clock and never reads the challenge ts — a signature
    # built with a signedAt an hour off the broker's own clock must still verify.
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    now_ms = int(time.time() * 1000)
    for signed_at in (now_ms - 3_600_000, now_ms + 3_600_000):  # one hour in the past / future
        params = {
            "client": {"id": "node-host", "mode": "node", "platform": "macos"},
            "role": "node",
            "scopes": [],
            "auth": {"token": "t"},
            "device": {"id": "d", "signedAt": signed_at, "nonce": "n", "publicKey": _b64u(raw)},
        }
        params["device"]["signature"] = _b64u(key.sign(node_broker.device_payload_v3(params, "n").encode()))
        assert node_broker.signature_matches(params, "n") is True


def test_params_json_is_a_string_not_an_object():
    frame = node_broker.invoke_request("inv-1", "dev-1", "system.run", {"command": ["bash", "-lc", "ls"]}, 120_000)
    assert frame == {
        "type": "event",
        "event": "node.invoke.request",
        "payload": {
            "id": "inv-1",
            "nodeId": "dev-1",
            "command": "system.run",
            "paramsJSON": json.dumps({"command": ["bash", "-lc", "ls"]}),
            "timeoutMs": 120_000,
        },
    }
    assert isinstance(frame["payload"]["paramsJSON"], str)


async def test_a_result_resolves_its_own_invoke_and_is_acked():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    task = asyncio.create_task(session.invoke("system.run", {"command": ["ls"]}, 5_000))
    await asyncio.sleep(0)
    invoke_id = next(iter(session.pending))
    ack = session.resolve(
        {
            "type": "req",
            "id": "r-9",
            "method": "node.invoke.result",
            "params": {
                "id": invoke_id,
                "nodeId": "dev-1",
                "ok": True,
                "payloadJSON": json.dumps({"stdout": "README.md", "exitCode": 0}),
            },
        }
    )
    assert ack == {"type": "res", "id": "r-9", "ok": True}
    assert (await task) == {"ok": True, "payload": {"stdout": "README.md", "exitCode": 0}}
    assert session.pending == {}


async def test_a_late_or_unknown_result_is_acked_and_dropped_not_raised():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    assert session.resolve(
        {
            "type": "req",
            "id": "r-1",
            "method": "node.invoke.result",
            "params": {"id": "ghost", "ok": True},
        }
    ) == {"type": "res", "id": "r-1", "ok": True}


async def test_a_timeout_clears_the_pending_entry_so_the_map_cannot_grow():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    with pytest.raises(node_broker.NodeInvokeTimeout):
        await session.invoke("system.run", {"command": ["sleep", "9"]}, 50)
    assert session.pending == {}


async def test_timeout_is_exact_and_a_late_result_after_it_is_ignored_not_raised():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    start = time.monotonic()
    task = asyncio.create_task(session.invoke("system.run", {"command": ["sleep", "9"]}, 10))
    await asyncio.sleep(0)
    invoke_id = next(iter(session.pending))
    with pytest.raises(node_broker.NodeInvokeTimeout):
        await task
    elapsed = time.monotonic() - start
    assert elapsed < 1.0  # NodeInvokeTimeout fires at timeout_ms/1000 — no undocumented +5s floor
    assert session.pending == {}
    # a result arriving after the deadline must be dropped cleanly, not raise
    ack = session.resolve(
        {
            "type": "req",
            "id": "late-1",
            "method": "node.invoke.result",
            "params": {"id": invoke_id, "ok": True, "payloadJSON": json.dumps({"stdout": "late"})},
        }
    )
    assert ack == {"type": "res", "id": "late-1", "ok": True}


async def test_a_command_the_device_never_declared_is_refused_before_it_reaches_the_socket():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    with pytest.raises(node_broker.CommandNotAllowed):
        await session.invoke("camera.capture", {}, 1_000)


async def test_fail_pending_raises_node_detached_immediately_not_after_timeout_ms():
    """Fix round 1, item 4: closing the socket must not leave an in-flight invoke waiting
    out its full timeout_ms for a reply that can never arrive."""
    session = node_broker.NodeSession("dev-1", {"system.run"})
    task = asyncio.create_task(session.invoke("system.run", {"command": ["sleep", "9"]}, 60_000))
    await asyncio.sleep(0)
    assert session.pending  # the invoke is genuinely in flight

    start = time.monotonic()
    session.fail_pending(node_broker.NodeDetached("socket closed"))
    with pytest.raises(node_broker.NodeDetached):
        await task
    assert time.monotonic() - start < 1.0  # nowhere near the 60s timeout_ms
    assert session.pending == {}


async def test_fail_pending_is_a_no_op_with_nothing_in_flight():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    session.fail_pending(node_broker.NodeDetached("socket closed"))  # must not raise
    assert session.pending == {}


async def test_progress_and_node_event_are_acked_and_need_no_implementation():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    for method in ("node.invoke.progress", "node.event"):
        assert session.resolve({"type": "req", "id": "x", "method": method, "params": {}}) == {
            "type": "res",
            "id": "x",
            "ok": True,
        }


async def test_cancel_uses_verified_invoke_id_contract_without_claiming_termination():
    session = node_broker.NodeSession("dev-1", {"device.info"})
    task = asyncio.create_task(session.invoke("device.info", {}, 5000, invoke_id="known-id"))
    await asyncio.sleep(0)
    await session.send.get()
    assert session.cancel("known-id") is True
    assert await session.send.get() == {
        "type": "event",
        "event": "node.invoke.cancel",
        "payload": {"invokeId": "known-id", "nodeId": "dev-1"},
    }
    assert not task.done()
    session.fail_pending(node_broker.NodeDetached())
    with pytest.raises(node_broker.NodeDetached):
        await task


async def test_pending_and_outbound_queue_are_bounded_and_timeout_drops_queued_invoke():
    session = node_broker.NodeSession("dev-1", {"device.info"})
    tasks = [asyncio.create_task(session.invoke("device.info", {}, 5000)) for _ in range(8)]
    await asyncio.sleep(0)
    with pytest.raises(node_broker.NodeCapacity):
        await session.invoke("device.info", {}, 5000)
    assert len(session.pending) == 8
    session.fail_pending(node_broker.NodeDetached())
    await asyncio.gather(*tasks, return_exceptions=True)
    assert session.send.empty()
    with pytest.raises(node_broker.NodeInvokeTimeout):
        await session.invoke("device.info", {}, 1)
    assert session.send.empty()


async def test_cancelled_waiter_sends_exact_cancel_after_request_left_queue():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    task = asyncio.create_task(session.invoke("system.run", {}, 5000, invoke_id="execution-id"))
    await session.send.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not session.pending
    assert session.send.get_nowait() == {
        "type": "event",
        "event": "node.invoke.cancel",
        "payload": {"invokeId": "execution-id", "nodeId": "dev-1"},
    }


async def test_oversize_wire_json_is_rejected_before_nested_decode():
    session = node_broker.NodeSession("dev-1", {"device.info"})
    task = asyncio.create_task(session.invoke("device.info", {}, 5000, invoke_id="known-id"))
    await asyncio.sleep(0)
    session.resolve(
        {
            "type": "req",
            "id": "result",
            "method": "node.invoke.result",
            "params": {"id": "known-id", "ok": True, "payloadJSON": '"' + "x" * (1024 * 1024) + '"'},
        }
    )
    with pytest.raises(node_broker.NodeResultTooLarge):
        await task


@pytest.mark.parametrize(
    "fields",
    [
        {"payloadJSON": "{}", "payload": "x" * (2 * 1024 * 1024)},
        {"payloadJSON": "{}", "error": {"message": "x" * (2 * 1024 * 1024)}},
        {"payload": "x" * 600000, "error": {"message": "x" * 600000}},
    ],
)
async def test_all_retained_result_fields_share_one_byte_budget(fields):
    session = node_broker.NodeSession("dev-1", {"device.info"})
    task = asyncio.create_task(session.invoke("device.info", {}, 5000, invoke_id="known-id"))
    await asyncio.sleep(0)
    session.resolve(
        {
            "type": "req",
            "id": "result",
            "method": "node.invoke.result",
            "params": {"id": "known-id", "ok": True, **fields},
        }
    )
    with pytest.raises(node_broker.NodeResultTooLarge):
        await task


# --- the CDP channel (browser.cdp.*) -------------------------------------------------------


def _cdp_session() -> node_broker.NodeSession:
    return node_broker.NodeSession("mac-1", {node_broker.CDP_COMMAND, "device.info"})


def _mac(event: str, **payload) -> dict:
    return {"type": "event", "event": f"browser.cdp.{event}", "payload": payload}


async def _opened(session: node_broker.NodeSession) -> node_broker.CdpChannel:
    task = asyncio.create_task(session.cdp_open("prof-1"))
    frame = await session.cdp_out.get()
    assert session.resolve(_mac("opened", channel=frame["payload"]["channel"])) is None
    return await task


async def test_cdp_open_sends_open_event_and_waits_for_opened():
    session = _cdp_session()
    task = asyncio.create_task(session.cdp_open("prof-1"))
    frame = await session.cdp_out.get()
    channel_id = frame["payload"]["channel"]
    assert frame == {
        "type": "event",
        "event": "browser.cdp.open",
        "payload": {"channel": channel_id, "nodeId": "mac-1", "profileId": "prof-1"},
    }
    await asyncio.sleep(0)
    assert not task.done()  # waits for the Mac's opened
    session.resolve(_mac("opened", channel=channel_id))
    channel = await task
    assert channel.id == channel_id and session.cdp == {channel_id: channel}


async def test_cdp_open_refused_by_mac_raises_with_code():
    session = _cdp_session()
    task = asyncio.create_task(session.cdp_open("prof-1"))
    frame = await session.cdp_out.get()
    session.resolve(_mac("close", channel=frame["payload"]["channel"], code="BROWSER_OFF"))
    with pytest.raises(node_broker.CdpClosed) as err:
        await task
    assert err.value.code == "BROWSER_OFF"
    assert session.cdp == {} and session.cdp_out.empty()  # the Mac's close is not echoed


async def test_cdp_open_times_out_and_sends_close(monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_OPEN_TIMEOUT_S", 0.01)
    session = _cdp_session()
    with pytest.raises(node_broker.CdpClosed) as err:
        await session.cdp_open("prof-1")
    assert err.value.code == "TIMEOUT"
    open_frame, close_frame = session.cdp_out.get_nowait(), session.cdp_out.get_nowait()
    assert close_frame == {
        "type": "event",
        "event": "browser.cdp.close",
        "payload": {"channel": open_frame["payload"]["channel"], "code": "TIMEOUT"},
    }
    assert session.cdp == {}


async def test_cdp_open_requires_granted_command():
    session = node_broker.NodeSession("mac-1", {"device.info"})
    with pytest.raises(node_broker.CommandNotAllowed):
        await session.cdp_open("prof-1")
    assert session.cdp_out.empty()


async def test_cdp_channel_cap():
    session = _cdp_session()
    for _ in range(node_broker.MAX_CDP_CHANNELS):
        await _opened(session)
    with pytest.raises(node_broker.NodeCapacity):
        await session.cdp_open("prof-1")
    session.cdp_close(next(iter(session.cdp.values())))
    while not session.cdp_out.empty():
        session.cdp_out.get_nowait()
    for _ in range(node_broker.CDP_SEND_QUEUE):
        session.cdp_out.put_nowait({})
    with pytest.raises(node_broker.NodeCapacity):  # a full socket admits no open either
        await session.cdp_open("prof-1")


async def test_cdp_frames_route_to_their_channel_only():
    session = _cdp_session()
    a, b = await _opened(session), await _opened(session)
    assert session.resolve(_mac("frame", channel=a.id, data='{"id":1}')) is None
    assert session.resolve(_mac("frame", channel="ghost", data='{"id":9}')) is None
    assert await a.next() == '{"id":1}'
    assert a.inbound.empty() and b.inbound.empty()
    session.cdp_send(b, '{"id":2}')
    assert session.cdp_out.get_nowait() == _mac("frame", channel=b.id, data='{"id":2}')
    session.resolve(_mac("close", channel=a.id, code="CHROME_EXITED"))
    assert await a.next() is None and a.close_code == "CHROME_EXITED"
    assert not b.closed.is_set()


async def test_cdp_oversized_mac_frame_closes_channel(monkeypatch):
    monkeypatch.setattr(node_broker, "MAX_CDP_FRAME", 8)
    session = _cdp_session()
    channel, bad = await _opened(session), await _opened(session)
    assert session.resolve(_mac("frame", channel=channel.id, data="x" * 9)) is None
    assert session.resolve(_mac("frame", channel=bad.id, data={"not": "a string"})) is None
    for ch in (channel, bad):
        assert ch.closed.is_set() and ch.close_code == "TOO_LARGE" and ch.inbound.empty()
    assert session.cdp == {}


async def test_cdp_inbound_overflow_closes_with_overflow_without_raising():
    session = _cdp_session()
    channel = await _opened(session)
    for i in range(node_broker.CDP_INBOUND_QUEUE):
        session.resolve(_mac("frame", channel=channel.id, data=str(i)))
    assert session.resolve(_mac("frame", channel=channel.id, data="one too many")) is None
    assert channel.close_code == "OVERFLOW" and channel.closed.is_set()
    assert session.cdp_out.get_nowait()["payload"] == {"channel": channel.id, "code": "OVERFLOW"}
    # the frames already queued still drain before the end
    assert [await channel.next() for _ in range(2)] == ["0", "1"]


async def test_cdp_send_rejects_oversized_frame(monkeypatch):
    monkeypatch.setattr(node_broker, "MAX_CDP_FRAME", 8)
    session = _cdp_session()
    channel = await _opened(session)
    with pytest.raises(node_broker.CdpClosed) as err:
        session.cdp_send(channel, "é" * 5)  # 10 bytes, 5 chars
    assert err.value.code == "TOO_LARGE" and session.cdp_out.empty()
    session.cdp_close(channel)
    session.cdp_out.get_nowait()
    with pytest.raises(node_broker.CdpClosed):
        session.cdp_send(channel, "{}")


async def test_cdp_close_is_idempotent_and_sends_one_close_event():
    session = _cdp_session()
    channel = await _opened(session)
    session.cdp_close(channel)
    session.cdp_close(channel, "TIMEOUT")
    assert session.cdp_out.get_nowait() == _mac("close", channel=channel.id, code="CLOSED")
    assert session.cdp_out.empty()
    assert channel.close_code == "CLOSED" and channel.closed.is_set() and session.cdp == {}
    assert await channel.next() is None


async def test_fail_pending_closes_cdp_channels():
    session = _cdp_session()
    channel = await _opened(session)
    pending = asyncio.create_task(session.cdp_open("prof-1"))
    await asyncio.sleep(0)
    reader = asyncio.create_task(channel.next())
    await asyncio.sleep(0)
    session.fail_pending(node_broker.NodeDetached())
    with pytest.raises(node_broker.CdpClosed) as err:
        await pending
    assert err.value.code == "CLOSED"
    assert await reader is None
    assert channel.closed.is_set() and channel.close_code == "CLOSED" and session.cdp == {}
    assert session.cdp_out.empty()


async def test_invokes_still_admitted_while_cdp_frames_queued():
    session = _cdp_session()
    channel = await _opened(session)
    for _ in range(64):  # far past the 16-frame invoke lane
        session.cdp_send(channel, "{}")
    task = asyncio.create_task(session.invoke("device.info", {}, 5_000))
    await asyncio.sleep(0)
    assert len(session.pending) == 1
    session.fail_pending(node_broker.NodeDetached())
    with pytest.raises(node_broker.NodeDetached):
        await task


async def test_cdp_inbound_byte_budget_closes_with_overflow(monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_INBOUND_BYTES", 10)
    session = _cdp_session()
    channel = await _opened(session)
    session.resolve(_mac("frame", channel=channel.id, data="x" * 6))
    assert await channel.next() == "x" * 6 and channel.inbound.bytes == 0  # consumed bytes are released
    session.resolve(_mac("frame", channel=channel.id, data="x" * 6))
    assert session.resolve(_mac("frame", channel=channel.id, data="x" * 6)) is None
    assert channel.close_code == "OVERFLOW" and channel.closed.is_set()


async def test_cdp_send_byte_budget_raises_capacity_and_releases_on_send(monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_SEND_BYTES", 10)
    session = _cdp_session()
    channel = await _opened(session)
    session.cdp_send(channel, "x" * 6)
    with pytest.raises(node_broker.NodeCapacity):
        session.cdp_send(channel, "x" * 6)
    await session.next_outbound()  # the relay writer sends it
    assert session.cdp_out.bytes == 0
    session.cdp_send(channel, "x" * 6)


@pytest.mark.parametrize(
    "code, expected", [("CHROME_EXITED", "CHROME_EXITED"), ("x" * 5, "CLOSED"), ("A" * 33, "CLOSED"), (7, "CLOSED")]
)
async def test_cdp_mac_close_code_is_bounded(code, expected):
    session = _cdp_session()
    channel = await _opened(session)
    session.resolve(_mac("close", channel=channel.id, code=code))
    assert channel.close_code == expected


async def test_cancelled_cdp_open_frees_its_slot_and_sends_close():
    session = _cdp_session()
    task = asyncio.create_task(session.cdp_open("prof-1"))
    open_frame = await session.cdp_out.get()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.cdp == {}
    assert session.cdp_out.get_nowait() == _mac("close", channel=open_frame["payload"]["channel"], code="CLOSED")


# --- the CDP send lane ------------------------------------------------------------------------


def _invoke_frame(session, n):
    session.send.put_nowait({"type": "event", "event": "node.invoke.request", "payload": {"id": f"i{n}"}})


async def test_cdp_frames_skip_ahead_of_queued_invokes():
    session = _cdp_session()
    channel = await _opened(session)
    for n in range(3):
        _invoke_frame(session, n)
    session.cdp_send(channel, '{"id":1}')
    assert await session.next_outbound() == _mac("frame", channel=channel.id, data='{"id":1}')
    assert (await session.next_outbound())["payload"] == {"id": "i0"}


async def test_control_frame_not_starved_by_cdp_burst(monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_BURST", 3)
    session = _cdp_session()
    channel = await _opened(session)  # the open frame was taken by _opened, not next_outbound
    for _ in range(10):
        session.cdp_send(channel, "{}")
    _invoke_frame(session, 0)
    events = [(await session.next_outbound())["event"] for _ in range(5)]
    assert events == ["browser.cdp.frame"] * 3 + ["node.invoke.request", "browser.cdp.frame"]


async def test_next_outbound_waits_for_either_lane():
    session = _cdp_session()
    waiter = asyncio.create_task(session.next_outbound())
    await asyncio.sleep(0)
    assert not waiter.done()
    _invoke_frame(session, 0)
    assert (await asyncio.wait_for(waiter, 1))["payload"] == {"id": "i0"}


async def test_cdp_lane_byte_budget_raises_capacity(monkeypatch):
    monkeypatch.setattr(node_broker, "CDP_SEND_BYTES", 10)
    session = _cdp_session()
    channel = await _opened(session)
    _invoke_frame(session, 0)  # control-lane frames never count against the CDP budget
    session.cdp_send(channel, "x" * 10)
    with pytest.raises(node_broker.NodeCapacity):
        session.cdp_send(channel, "x")


async def test_invoke_capacity_unaffected_by_cdp_backlog():
    session = _cdp_session()
    channel = await _opened(session)
    for _ in range(node_broker.CDP_SEND_QUEUE):
        session.cdp_send(channel, "{}")
    with pytest.raises(node_broker.NodeCapacity):
        session.cdp_send(channel, "{}")
    task = asyncio.create_task(session.invoke("device.info", {}, 5_000, invoke_id="x"))
    await asyncio.sleep(0)
    assert session.send.get_nowait()["payload"]["id"] == "x"
    for _ in range(node_broker.SEND_QUEUE):
        session.send.put_nowait({})
    with pytest.raises(node_broker.NodeCapacity):  # a full invoke lane still refuses at 16
        await session.invoke("device.info", {}, 5_000)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_fail_pending_drains_cdp_lane():
    session = _cdp_session()
    channel = await _opened(session)
    session.cdp_send(channel, "x" * 5)
    _invoke_frame(session, 0)
    session.fail_pending(node_broker.NodeDetached())
    assert session.cdp_out.empty() and session.send.empty() and session.cdp_out.bytes == 0


# --- the cast lane (browser.cast.*) ---------------------------------------------------------


def _cast_session() -> node_broker.NodeSession:
    return node_broker.NodeSession("mac-1", {node_broker.CDP_COMMAND, node_broker.CAST_COMMAND, "device.info"})


def _cast_frame_evt(**payload) -> dict:
    return {"type": "event", "event": "browser.cast.frame", "payload": payload}


def _valid_frame(**overrides) -> dict:
    return {"tier": "view", "jpegBase64": "AAAA", "w": 100, "h": 50, "seq": 1, **overrides}


async def test_a_valid_frame_lands_in_the_slot_and_a_newer_frame_replaces_it():
    session = _cast_session()
    channel = await _opened(session)
    assert session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame())) is None
    assert channel.cast["view"] == {"jpegBase64": "AAAA", "w": 100, "h": 50, "seq": 1}
    assert channel.cast_ready.is_set()
    assert session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame(jpegBase64="BBBB", seq=2))) is None
    # a slot, not a queue: the newer frame replaces the older one, not appends
    assert channel.cast["view"] == {"jpegBase64": "BBBB", "w": 100, "h": 50, "seq": 2}
    assert len(channel.cast) == 1


async def test_view_and_thumb_tiers_hold_independent_slots():
    session = _cast_session()
    channel = await _opened(session)
    session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame(tier="view", seq=1)))
    session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame(tier="thumb", seq=2)))
    assert channel.cast["view"]["seq"] == 1
    assert channel.cast["thumb"]["seq"] == 2


@pytest.mark.parametrize(
    "overrides",
    [
        {"tier": "full"},  # not in {view, thumb}
        {"jpegBase64": 12345},  # not a str
        {"jpegBase64": "x" * (node_broker.MAX_FRAME_B64 + 1)},  # over the byte bound
        {"w": 0},  # below 1..10000
        {"w": 10001},  # above 1..10000
        {"w": True},  # bool is not an int here
        {"h": 0},
        {"h": "50"},  # not an int
        {"seq": 1.5},  # not an int
        {"seq": True},  # bool is not an int here
    ],
)
async def test_invalid_or_oversize_frames_are_dropped_and_the_channel_stays_open(overrides):
    session = _cast_session()
    channel = await _opened(session)
    assert session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame(**overrides))) is None
    assert channel.cast == {}
    assert not channel.cast_ready.is_set()
    assert not channel.closed.is_set()
    assert session.cdp == {channel.id: channel}


async def test_a_frame_is_dropped_when_the_session_lacks_the_cast_command():
    # The capability gate must hold both ways: a session that never declared browser.cast.v1
    # can still hold a CDP channel (browser.cdp.v1 alone is enough to open one), so an inbound
    # browser.cast.frame from such a session must be dropped just like an invalid one.
    session = node_broker.NodeSession("mac-1", {node_broker.CDP_COMMAND, "device.info"})
    assert session.can_cast is False
    channel = await _opened(session)
    assert session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame())) is None
    assert channel.cast == {}
    assert not channel.cast_ready.is_set()
    assert not channel.closed.is_set()


async def test_a_frame_for_an_unknown_or_closed_channel_is_ignored():
    session = _cast_session()
    channel = await _opened(session)
    session.cdp_close(channel)
    assert session.resolve(_cast_frame_evt(channel=channel.id, **_valid_frame())) is None
    assert channel.cast == {}
    assert session.resolve(_cast_frame_evt(channel="ghost", **_valid_frame())) is None


async def test_cast_set_and_cast_input_enqueue_the_exact_event_shapes():
    session = _cast_session()
    channel = await _opened(session)
    session.cast_set(channel, view=True, thumb=False)
    assert session.cdp_out.get_nowait() == {
        "type": "event",
        "event": "browser.cast.set",
        "payload": {"channel": channel.id, "view": True, "thumb": False},
    }
    events = [{"type": "down", "x": 1, "y": 2, "button": "left", "clicks": 1}]
    session.cast_input(channel, events)
    assert session.cdp_out.get_nowait() == {
        "type": "event",
        "event": "browser.cast.input",
        "payload": {"channel": channel.id, "events": events},
    }


async def test_cast_set_and_cast_input_refuse_without_the_command():
    session = node_broker.NodeSession("mac-1", {node_broker.CDP_COMMAND, "device.info"})
    channel = await _opened(session)
    assert session.can_cast is False
    with pytest.raises(node_broker.CommandNotAllowed):
        session.cast_set(channel, view=True, thumb=True)
    with pytest.raises(node_broker.CommandNotAllowed):
        session.cast_input(channel, [])
    assert session.cdp_out.empty()


async def test_can_cast_reflects_the_granted_commands():
    assert _cast_session().can_cast is True
    assert node_broker.NodeSession("mac-1", {node_broker.CDP_COMMAND}).can_cast is False


async def test_cast_set_and_cast_input_raise_cdp_closed_on_a_closed_channel():
    session = _cast_session()
    channel = await _opened(session)
    session.cdp_close(channel, "CHROME_EXITED")
    with pytest.raises(node_broker.CdpClosed) as err:
        session.cast_set(channel, view=True, thumb=True)
    assert err.value.code == "CHROME_EXITED"
    with pytest.raises(node_broker.CdpClosed):
        session.cast_input(channel, [])


async def test_cdp_open_carries_a_paperclip_notice():
    session = _cdp_session()
    task = asyncio.create_task(session.cdp_open("prof-1", "Friday is using your Mac's browser for a team task."))
    frame = await session.next_outbound()
    assert frame["payload"]["notice"] == "Friday is using your Mac's browser for a team task."
    session.resolve(_mac("opened", channel=frame["payload"]["channel"]))
    await task
    plain = asyncio.create_task(session.cdp_open("prof-1"))
    assert "notice" not in (await session.next_outbound())["payload"]
    plain.cancel()


SID = "11111111-1111-4111-8111-111111111111"
OFFER = "a" * 32


def _offer(session, cid=OFFER, sid=SID, mac=True):
    session.resolve(
        {
            "type": "event",
            "event": "browser.cdp.offer",
            "payload": {"channel": cid, "sessionId": sid, "macBrowser": mac},
        }
    )


def _out(session):
    frames = []
    while not session.cdp_out.empty():
        frames.append(session.cdp_out.get_nowait())
    return frames


async def test_an_offer_registers_the_macs_channel_and_hands_it_to_the_relay():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    seen = []
    session.on_offer = lambda channel, sid, mac: seen.append((channel.id, sid, mac))
    _offer(session)
    _offer(session)  # a replay changes nothing
    assert seen == [(OFFER, SID, True)] and session.cdp[OFFER].id == OFFER


@pytest.mark.parametrize(("cid", "sid"), [("../x", SID), (OFFER, "not-a-session"), (OFFER.upper(), SID)])
async def test_a_malformed_offer_is_ignored(cid, sid):
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: pytest.fail("offered")
    _offer(session, cid=cid, sid=sid)
    assert session.cdp == {} and _out(session) == []


async def test_an_offer_past_the_channel_cap_is_refused_busy():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    for i in range(node_broker.MAX_CDP_CHANNELS):
        _offer(session, cid=f"{i:032x}")
    _offer(session, cid="f" * 32)
    assert _out(session) == [
        {
            "type": "event",
            "event": "browser.cdp.close",
            "payload": {"channel": "f" * 32, "code": "mac_browser_busy", "message": node_broker.BUSY_MESSAGE},
        }
    ]
    assert "f" * 32 not in session.cdp


async def test_a_relayed_offer_opens_when_the_mac_says_opened_and_then_carries_its_frames():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session, mac=False)
    channel = session.cdp[OFFER]
    accepted = asyncio.create_task(session.cdp_accept(channel, {"relay": True}))
    await asyncio.sleep(0)
    assert _out(session) == [
        {"type": "event", "event": "browser.cdp.open", "payload": {"channel": OFFER, "nodeId": "mac1", "relay": True}}
    ]
    assert not accepted.done()  # waits for the Mac's opened (the desktop relay sends it)
    session.resolve(_mac("opened", channel=OFFER))
    await asyncio.wait_for(accepted, 1)
    session.resolve(_mac("frame", channel=OFFER, data='{"id":1}'))
    assert channel.inbound.get_nowait() == '{"id":1}'


async def test_accept_on_an_already_closed_channel_raises_and_opens_nothing():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    channel = session.cdp[OFFER]
    session.cdp_close(channel, "GONE")
    _out(session)
    with pytest.raises(node_broker.CdpClosed) as err:
        await session.cdp_accept(channel, {"profileId": "p1"})
    assert err.value.code == "GONE"
    assert _out(session) == []


async def test_accept_opens_the_offered_channel_and_waits_for_opened():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    accepted = asyncio.create_task(session.cdp_accept(session.cdp[OFFER], {"profileId": "p1"}))
    await asyncio.sleep(0)
    assert _out(session) == [
        {
            "type": "event",
            "event": "browser.cdp.open",
            "payload": {"channel": OFFER, "nodeId": "mac1", "profileId": "p1"},
        }
    ]
    session.resolve({"type": "event", "event": "browser.cdp.opened", "payload": {"channel": OFFER}})
    await asyncio.wait_for(accepted, 1)


async def test_a_local_channels_cdp_frames_are_never_queued():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    channel = session.cdp[OFFER]
    channel.local = True
    session.resolve({"type": "event", "event": "browser.cdp.frame", "payload": {"channel": OFFER, "data": "{}"}})
    assert channel.inbound.empty() and OFFER in session.cdp


async def test_a_close_can_carry_the_message_a_session_is_shown():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    session.cdp_close(session.cdp[OFFER], "not_shared", "Share this session to use the Lightbulb browser")
    assert _out(session)[-1]["payload"] == {
        "channel": OFFER,
        "code": "not_shared",
        "message": "Share this session to use the Lightbulb browser",
    }


async def test_idle_and_active_reach_the_channels_holder_and_a_close_wakes_it():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    channel = session.cdp[OFFER]
    for kind in ("idle", "active"):
        session.resolve({"type": "event", "event": f"browser.cdp.{kind}", "payload": {"channel": OFFER}})
    session.resolve({"type": "event", "event": "browser.cdp.close", "payload": {"channel": OFFER, "code": "CLOSED"}})
    assert [channel.signals.get_nowait() for _ in range(3)] == ["idle", "active", "closed"]


async def test_leased_answers_the_mac_with_the_code_when_it_failed():
    session = node_broker.NodeSession("mac1", {"browser.cdp.v1"})
    session.on_offer = lambda *args: None
    _offer(session)
    session.cdp_leased(session.cdp[OFFER], True)
    session.cdp_leased(session.cdp[OFFER], False, "mac_browser_busy", node_broker.BUSY_MESSAGE)
    assert [f["payload"] for f in _out(session)] == [
        {"channel": OFFER, "ok": True},
        {"channel": OFFER, "ok": False, "code": "mac_browser_busy", "message": node_broker.BUSY_MESSAGE},
    ]
