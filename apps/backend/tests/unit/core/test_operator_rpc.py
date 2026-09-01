"""Tests for the shared signed operator-connect helper.

`core.gateway.operator_rpc` is the single canonical place that performs the
OpenClaw 4.5 signed-device connect handshake. The connection pool, the
credential-revoke service, the recovery endpoint, and the gateway-restart
endpoint all route their signed connect through it instead of carrying their
own copy.

These tests stub the same dependencies the pool's handshake test stubs:

- ``container_repo.get_by_owner_id`` → a row carrying an
  ``operator_priv_key_enc`` ciphertext (opaque; KMS is mocked).
- ``kms_secrets.decrypt_bytes`` → a real 32-byte Ed25519 seed so
  ``sign_connect_request`` produces a signature we can verify.
"""

import json
import uuid as uuid_mod
from unittest.mock import AsyncMock, patch

import pytest

from core.gateway import operator_rpc


@pytest.fixture
def operator_seed():
    """Fresh 32-byte Ed25519 seed for the test run."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
    )

    priv = Ed25519PrivateKey.generate()
    return priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())


@pytest.fixture
def mock_device_auth_deps(operator_seed):
    """Patch container_repo + kms_secrets so the helper can load an identity."""
    with (
        patch("core.repositories.container_repo.get_by_owner_id", new_callable=AsyncMock) as mock_get,
        patch("core.crypto.kms_secrets.decrypt_bytes") as mock_decrypt,
    ):
        mock_get.return_value = {
            "owner_id": "test-user",
            "operator_priv_key_enc": "opaque-kms-ciphertext-blob",
            "gateway_token": "test-token",
        }
        mock_decrypt.return_value = operator_seed
        yield mock_get, mock_decrypt


class _FakeWsCM:
    """Minimal async-context-manager wrapper around a fake websocket."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


# --------------------------------------------------------------------------
# perform_signed_connect
# --------------------------------------------------------------------------


class TestPerformSignedConnect:
    @pytest.mark.asyncio
    async def test_sends_signed_device_and_returns_hello(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "nonce-abc"}}),
                json.dumps({"ok": True, "payload": {"protocol": 4}}),
            ]
        )

        hello = await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="test-token")

        sent = json.loads(ws.send.call_args[0][0])
        params = sent["params"]
        assert sent["method"] == "connect"
        assert params["role"] == "operator"
        assert params["minProtocol"] == 4
        assert params["maxProtocol"] == 4
        assert params["scopes"] == ["operator.admin", "operator.read", "operator.write"]
        assert params["auth"]["token"] == "test-token"
        assert params["client"]["id"] == "gateway-client"
        assert params["client"]["mode"] == "backend"

        device = params["device"]
        assert set(device.keys()) == {"id", "publicKey", "signature", "signedAt", "nonce"}
        assert device["nonce"] == "nonce-abc"
        assert device["signature"]

        # Signature verifies against the v2 payload the server will rebuild.
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from core.crypto.operator_device import (
            build_v2_connect_payload,
            _b64url_nopad_decode,
        )

        expected = build_v2_connect_payload(
            device_id=device["id"],
            client_id="gateway-client",
            client_mode="backend",
            role="operator",
            scopes=["operator.admin", "operator.read", "operator.write"],
            signed_at_ms=device["signedAt"],
            token="test-token",
            nonce="nonce-abc",
        )
        pub = Ed25519PublicKey.from_public_bytes(_b64url_nopad_decode(device["publicKey"]))
        pub.verify(_b64url_nopad_decode(device["signature"]), expected.encode("utf-8"))

        assert hello == {"ok": True, "payload": {"protocol": 4}}

    @pytest.mark.asyncio
    async def test_no_caps_key_by_default(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
            ]
        )
        await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        params = json.loads(ws.send.call_args[0][0])["params"]
        assert "caps" not in params

    @pytest.mark.asyncio
    async def test_includes_caps_when_passed(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
            ]
        )
        await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t", caps=["tool-events"])
        params = json.loads(ws.send.call_args[0][0])["params"]
        assert params["caps"] == ["tool-events"]

    @pytest.mark.asyncio
    async def test_raises_on_wrong_first_event(self):
        ws = AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({"event": "something.else"}))
        with pytest.raises(RuntimeError, match="Expected connect.challenge"):
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")

    @pytest.mark.asyncio
    async def test_raises_when_gateway_rejects(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": False, "error": {"message": "invalid token"}}),
            ]
        )
        with pytest.raises(RuntimeError, match="Gateway connect failed: invalid token"):
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")

    @pytest.mark.asyncio
    async def test_auth_shaped_rejection_raises_typed_error(self, mock_device_auth_deps):
        """An error code containing AUTH (AUTH_FAILED / AUTH_RATE_LIMITED)
        must surface as GatewayConnectAuthError so the pool can hold off
        reconnects — the gateway locks a client scope out for 5 minutes after
        10 failed auths in 60s, and a plain RuntimeError is indistinguishable
        from a transient handshake failure."""
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps(
                    {
                        "ok": False,
                        "error": {"message": "rate limited", "code": "AUTH_RATE_LIMITED", "retryAfterMs": 30000},
                    }
                ),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectAuthError, match="rate limited") as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert ei.value.code == "AUTH_RATE_LIMITED"
        assert ei.value.retry_after_ms == 30000
        # Still a RuntimeError so pre-existing broad handlers keep catching it.
        assert isinstance(ei.value, RuntimeError)

    @pytest.mark.asyncio
    async def test_auth_error_retry_after_nested_under_data(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps(
                    {
                        "ok": False,
                        "error": {"message": "bad token", "code": "AUTH_FAILED", "data": {"retryAfterMs": 1500}},
                    }
                ),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectAuthError) as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert ei.value.code == "AUTH_FAILED"
        assert ei.value.retry_after_ms == 1500

    @pytest.mark.asyncio
    async def test_auth_error_without_retry_after(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": False, "error": {"message": "bad token", "code": "AUTH_FAILED"}}),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectAuthError) as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert ei.value.code == "AUTH_FAILED"
        assert ei.value.retry_after_ms is None

    @pytest.mark.asyncio
    async def test_non_auth_rejection_stays_plain_runtime_error(self, mock_device_auth_deps):
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": False, "error": {"message": "unsupported", "code": "PROTOCOL_MISMATCH"}}),
            ]
        )
        with pytest.raises(RuntimeError, match="Gateway connect failed: unsupported") as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert not isinstance(ei.value, operator_rpc.GatewayConnectAuthError)
        # A rejection with no retry signal is genuinely deterministic — it must
        # NOT pick up the transient type, or the bootstrap dispatcher would
        # retry a protocol mismatch on a ladder that cannot fix it.
        assert not isinstance(ei.value, operator_rpc.GatewayConnectUnavailableError)

    @pytest.mark.asyncio
    async def test_startup_window_rejection_raises_the_transient_type(self, mock_device_auth_deps):
        """THE PROD DEFECT. OpenClaw answers the handshake during its own boot
        window with UNAVAILABLE / retryable / "gateway starting; retry
        shortly". That arrived here as a BARE RuntimeError, so the bootstrap
        dispatcher (which matches transients by TYPE) skipped its ~3-minute
        connect ladder entirely and wrote bootstrap_state="failed" on the first
        hit — permanently dead-ending three prod owners' onboarding."""
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "message": "gateway starting; retry shortly",
                            "code": "UNAVAILABLE",
                            "retryable": True,
                            "retryAfterMs": 2000,
                        },
                    }
                ),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectUnavailableError, match="gateway starting") as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert ei.value.code == "UNAVAILABLE"
        assert ei.value.retry_after_ms == 2000
        # Still a RuntimeError so pre-existing broad handlers keep catching it.
        assert isinstance(ei.value, RuntimeError)

    @pytest.mark.asyncio
    async def test_retryable_flag_alone_is_enough(self, mock_device_auth_deps):
        """The classification is keyed on the gateway's STRUCTURED signal, not
        on message text (which is free-form and differs per call site — at
        least two distinct texts produced the prod dead-end). Either half of
        that signal is sufficient, so a gateway version that sends one without
        the other is still classified correctly."""
        for error in (
            {"message": "session store is busy", "code": "SOMETHING_ELSE", "retryable": True},
            {"message": "gateway starting", "code": "UNAVAILABLE"},
        ):
            ws = AsyncMock()
            ws.recv = AsyncMock(
                side_effect=[
                    json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                    json.dumps({"ok": False, "error": error}),
                ]
            )
            with pytest.raises(operator_rpc.GatewayConnectUnavailableError):
                await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")

    @pytest.mark.asyncio
    async def test_auth_rejection_wins_over_a_retryable_flag(self, mock_device_auth_deps):
        """An auth rate-limit may well set `retryable`, but each retry still
        counts toward the gateway's 10-in-60s lockout, so it must stay an auth
        error rather than become something callers ride a ladder against."""
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps(
                    {
                        "ok": False,
                        "error": {"message": "rate limited", "code": "AUTH_RATE_LIMITED", "retryable": True},
                    }
                ),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectAuthError) as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert not isinstance(ei.value, operator_rpc.GatewayConnectUnavailableError)

    @pytest.mark.asyncio
    async def test_raises_when_no_operator_seed(self):
        ws = AsyncMock()
        ws.recv = AsyncMock(return_value=json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}))
        with patch("core.repositories.container_repo.get_by_owner_id", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = {"owner_id": "test-user"}  # no operator_priv_key_enc
            with pytest.raises(RuntimeError, match="operator_priv_key_enc"):
                await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")

    @pytest.mark.asyncio
    async def test_not_paired_rejection_carries_details(self, mock_device_auth_deps):
        """NOT_PAIRED surfaces as GatewayConnectNotPairedError with the pending
        pairing requestId riding on .details — the pairing seam approves it."""
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "message": "pairing required: device is not approved yet",
                            "code": "NOT_PAIRED",
                            "details": {"requestId": "req-42", "reason": "not-paired"},
                        },
                    }
                ),
            ]
        )
        with pytest.raises(operator_rpc.GatewayConnectNotPairedError) as ei:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")
        assert ei.value.details["requestId"] == "req-42"

    @pytest.mark.asyncio
    async def test_decrypt_runs_off_the_event_loop_thread(self, operator_seed):
        """GW-2a: the synchronous KMS Decrypt must run in a worker thread, not
        inline on the connect hot path (which is held under the pool's single
        lock and would otherwise block the whole event loop for the round trip)."""
        import threading

        main_thread = threading.current_thread()
        seen: dict = {}

        def fake_decrypt(enc, *, encryption_context=None):
            seen["thread"] = threading.current_thread()
            return operator_seed

        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
            ]
        )
        with (
            patch("core.repositories.container_repo.get_by_owner_id", new_callable=AsyncMock) as mock_get,
            patch("core.crypto.kms_secrets.decrypt_bytes", side_effect=fake_decrypt),
        ):
            mock_get.return_value = {
                "owner_id": "test-user",
                "operator_priv_key_enc": "opaque-blob",
                "gateway_token": "t",
            }
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t")

        assert seen.get("thread") is not None
        assert seen["thread"] is not main_thread

    @pytest.mark.asyncio
    async def test_identity_override_skips_container_row_load(self, operator_seed):
        """An explicit identity (the pairing probe signing as a Paperclip
        agent's device) must bypass the DDB+KMS backend-device load entirely
        and sign the connect with the supplied key."""
        from core.crypto.operator_device import load_operator_device_from_seed

        identity = load_operator_device_from_seed(operator_seed)
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True, "payload": {}}),
            ]
        )
        with patch("core.repositories.container_repo.get_by_owner_id", new_callable=AsyncMock) as mock_get:
            await operator_rpc.perform_signed_connect(ws, owner_id="test-user", token="t", identity=identity)
            mock_get.assert_not_awaited()
        sent = json.loads(ws.send.await_args.args[0])
        assert sent["params"]["device"]["id"] == identity.device_id
        assert sent["params"]["device"]["publicKey"] == identity.public_key_b64


# --------------------------------------------------------------------------
# gateway_rpc_oneshot
# --------------------------------------------------------------------------


class TestGatewayRpcOneshot:
    @pytest.mark.asyncio
    async def test_returns_payload_on_success(self, mock_device_auth_deps):
        fixed = uuid_mod.UUID("12345678-1234-5678-1234-567812345678")
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
                json.dumps({"type": "res", "id": str(fixed), "ok": True, "payload": {"status": "ok"}}),
            ]
        )
        with (
            patch.object(operator_rpc.websockets, "connect", return_value=_FakeWsCM(ws)),
            patch.object(operator_rpc.uuid, "uuid4", return_value=fixed),
        ):
            payload = await operator_rpc.gateway_rpc_oneshot(
                "10.0.0.1", owner_id="test-user", token="t", method="health"
            )
        assert payload == {"status": "ok"}
        # The RPC frame was actually sent.
        rpc_frame = json.loads(ws.send.call_args_list[-1][0][0])
        assert rpc_frame["method"] == "health"

    @pytest.mark.asyncio
    async def test_raises_on_rpc_error(self, mock_device_auth_deps):
        fixed = uuid_mod.UUID("12345678-1234-5678-1234-567812345678")
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
                json.dumps({"type": "res", "id": str(fixed), "ok": False, "error": {"message": "nope"}}),
            ]
        )
        with (
            patch.object(operator_rpc.websockets, "connect", return_value=_FakeWsCM(ws)),
            patch.object(operator_rpc.uuid, "uuid4", return_value=fixed),
        ):
            with pytest.raises(RuntimeError, match="nope"):
                await operator_rpc.gateway_rpc_oneshot(
                    "10.0.0.1", owner_id="test-user", token="t", method="config.apply"
                )

    @pytest.mark.asyncio
    async def test_skips_event_broadcasts_before_response(self, mock_device_auth_deps):
        fixed = uuid_mod.UUID("12345678-1234-5678-1234-567812345678")
        ws = AsyncMock()
        ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"event": "connect.challenge", "payload": {"nonce": "n"}}),
                json.dumps({"ok": True}),
                json.dumps({"event": "health", "payload": {"noise": True}}),  # broadcast, ignored
                json.dumps({"type": "res", "id": "other-id", "ok": True, "payload": {}}),  # not ours
                json.dumps({"type": "res", "id": str(fixed), "ok": True, "payload": {"v": 1}}),
            ]
        )
        with (
            patch.object(operator_rpc.websockets, "connect", return_value=_FakeWsCM(ws)),
            patch.object(operator_rpc.uuid, "uuid4", return_value=fixed),
        ):
            payload = await operator_rpc.gateway_rpc_oneshot(
                "10.0.0.1", owner_id="test-user", token="t", method="health"
            )
        assert payload == {"v": 1}
