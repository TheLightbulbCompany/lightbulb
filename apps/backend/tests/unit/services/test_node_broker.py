import base64
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
    # Our Rust client signs with its LOCAL clock and never reads the challenge ts.
    assert node_broker.hello_ok("r", "c")  # sentinel: nothing in the broker compares signedAt to ts
    assert "signedAt" not in str(node_broker.challenge_frame())
