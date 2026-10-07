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
