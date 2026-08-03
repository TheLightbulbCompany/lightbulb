"""Shared OpenClaw signed operator-connect helper.

OpenClaw 4.5 replaced the pre-4.5 "token == admin" model with scoped auth: a
connect request that presents only ``auth.token`` (no signed device identity)
gets its self-declared scopes silently cleared by the server, leaving a
zero-permission connection. To actually receive ``operator.admin`` (needed for
``config.*``, ``update.*``, ``agents.*``, ``skills.*``, etc.) the backend must
sign the connect challenge with the per-container Ed25519 operator device.

That signed handshake used to be copy-pasted into four places (the connection
pool, the recovery endpoint, the credential-revoke service, and the
gateway-restart endpoint). This module is the single canonical implementation
they all share:

- ``perform_signed_connect`` runs the challenge → sign → hello-ok handshake on
  an already-open websocket. The long-lived connection pool uses this directly
  (it keeps the socket open afterwards and streams events).
- ``gateway_rpc_oneshot`` opens a short-lived websocket, runs
  ``perform_signed_connect``, sends a single RPC, and returns its payload. The
  one-shot operator calls (revoke, recover, restart) use this.

``GATEWAY_PORT`` lives here (not imported from ``core.containers.ecs_manager``)
to avoid a gateway → containers import cycle, mirroring the long-standing local
definition in ``connection_pool``.
"""

import asyncio
import json
import uuid

import websockets

GATEWAY_PORT = 18789  # OpenClaw gateway port (avoid circular import with containers/)

# Default per-recv timeout (seconds) for the short handshake/RPC exchanges.
_DEFAULT_TIMEOUT = 10


class GatewayConnectAuthError(RuntimeError):
    """Connect rejected with an auth-shaped error code (AUTH_FAILED,
    AUTH_RATE_LIMITED, …).

    Retrying these with the same credentials cannot succeed, and each retry
    counts toward the gateway's lockout (10 failed auths in 60s → 5-minute
    IP+credential-scope lockout), so the connection pool needs to tell them
    apart from transient handshake failures. ``retry_after_ms`` carries the
    gateway's own hint (the remaining lockout window) when it sends one.
    Subclasses RuntimeError so pre-existing broad handlers keep working.
    """

    def __init__(self, message: str, *, code: str | None = None, retry_after_ms: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_ms = retry_after_ms


class GatewayConnectNotPairedError(RuntimeError):
    """Connect rejected ``NOT_PAIRED`` — the signing device is not in the
    gateway's trust store.

    The rejection is not a dead end: the gateway persists a pending pairing
    request for the device and returns its id in the error ``details``
    (``connect-device-pairing.ts`` ``buildPairingConnectErrorDetails``). The
    pairing seam connects AS a freshly-minted device precisely to harvest that
    ``requestId`` and approve it over an admin connection. Subclasses
    RuntimeError so pre-existing broad handlers keep working.
    """

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details if isinstance(details, dict) else {}


async def perform_signed_connect(
    ws,
    *,
    owner_id: str,
    token: str,
    caps: list[str] | None = None,
    recv_timeout: float = _DEFAULT_TIMEOUT,
    scopes=None,
    identity=None,
) -> dict:
    """Run the OpenClaw 4.5 signed-device connect handshake on an open socket.

    Order matters: the challenge is read and validated *before* the operator
    device seed is loaded, so a non-challenge first frame fails fast without
    touching DynamoDB/KMS.

    1. Receive ``connect.challenge`` and extract its nonce.
    2. Load the per-container operator device identity (seed is KMS-encrypted in
       the containers DynamoDB row, bound by encryption context to ``owner_id``),
       unless the caller passed ``identity`` — the pairing probe connects AS a
       Paperclip agent's operator device (whose key the backend minted) to
       create a pending pairing request, so it must sign with that device, not
       the backend's.
    3. Sign the canonical v2 payload over the nonce and send the connect request
       with both the shared token AND the signed device.
    4. Verify ``hello-ok`` and return the parsed response.

    Raises ``GatewayConnectNotPairedError`` when the gateway rejects the device
    as unpaired (the pending-request details ride on the exception), and
    ``RuntimeError`` on any other handshake failure.
    """
    from core.crypto import kms_secrets
    from core.crypto.operator_device import (
        BACKEND_CLIENT_ID,
        BACKEND_CLIENT_MODE,
        BACKEND_OPERATOR_SCOPES,
        BACKEND_ROLE,
        load_operator_device_from_seed,
        sign_connect_request,
    )
    from core.repositories import container_repo

    if scopes is None:
        scopes = BACKEND_OPERATOR_SCOPES

    # Step 1: receive + validate the challenge (nonce for the signature).
    raw = await asyncio.wait_for(ws.recv(), timeout=recv_timeout)
    challenge = json.loads(raw)
    if challenge.get("event") != "connect.challenge":
        raise RuntimeError(f"Expected connect.challenge, got: {challenge.get('event', 'unknown')}")
    nonce = challenge.get("payload", {}).get("nonce") or challenge.get("nonce")
    if not nonce:
        raise RuntimeError(f"connect.challenge missing nonce: {challenge}")

    # Step 2: load the operator device identity for this container (unless the
    # caller signs as a different device — see the docstring).
    if identity is None:
        container = await container_repo.get_by_owner_id(owner_id)
        if not container:
            raise RuntimeError(f"No container row for owner {owner_id}")
        enc_seed = container.get("operator_priv_key_enc")
        if not enc_seed:
            raise RuntimeError(
                f"Container row for owner {owner_id} missing operator_priv_key_enc "
                "(pre-4.5 provision — reprovision required)"
            )
        seed_bytes = kms_secrets.decrypt_bytes(
            enc_seed,
            encryption_context={"owner_id": owner_id, "purpose": "operator-device-seed"},
        )
        identity = load_operator_device_from_seed(seed_bytes)

    # Step 3: sign the v2 payload with the nonce and send the connect request.
    device = sign_connect_request(
        identity=identity,
        token=token,
        nonce=nonce,
        scopes=scopes,
    )
    params = {
        "minProtocol": 4,
        "maxProtocol": 4,
        "client": {
            "id": BACKEND_CLIENT_ID,
            "version": "1.0.0",
            "platform": "linux",
            "mode": BACKEND_CLIENT_MODE,
        },
        "role": BACKEND_ROLE,
        "scopes": list(scopes),
        "auth": {"token": token},
        "device": device,
    }
    if caps:
        params["caps"] = list(caps)
    await ws.send(json.dumps({"type": "req", "id": str(uuid.uuid4()), "method": "connect", "params": params}))

    # Step 4: verify hello-ok.
    resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=recv_timeout))
    if not resp.get("ok"):
        error = resp.get("error")
        if not isinstance(error, dict):
            error = {}
        message = error.get("message", "unknown error")
        code = error.get("code")
        if code == "NOT_PAIRED":
            raise GatewayConnectNotPairedError(
                f"Gateway connect failed: {message}",
                details=error.get("details"),
            )
        if isinstance(code, str) and "AUTH" in code.upper():
            # retryAfterMs sits directly on the error object or nested under
            # error.data, depending on the gateway version.
            retry_after = error.get("retryAfterMs")
            if retry_after is None:
                data = error.get("data")
                if isinstance(data, dict):
                    retry_after = data.get("retryAfterMs")
            raise GatewayConnectAuthError(
                f"Gateway connect failed: {message}",
                code=code,
                retry_after_ms=int(retry_after) if isinstance(retry_after, (int, float)) else None,
            )
        raise RuntimeError(f"Gateway connect failed: {message}")
    return resp


async def gateway_rpc_oneshot(
    ip: str,
    *,
    owner_id: str,
    token: str,
    method: str,
    params: dict | None = None,
    caps: list[str] | None = None,
    open_timeout: float = _DEFAULT_TIMEOUT,
    recv_timeout: float = _DEFAULT_TIMEOUT,
    rpc_timeout: float = _DEFAULT_TIMEOUT,
    scopes=None,
) -> dict:
    """Open a short-lived signed connection, send one RPC, return its payload.

    Used by the rare one-shot operator calls (credential revoke, recovery,
    gateway restart). Raises ``RuntimeError`` if the handshake fails or the RPC
    response is not ``ok``.
    """
    uri = f"ws://{ip}:{GATEWAY_PORT}"
    async with websockets.connect(uri, open_timeout=open_timeout, close_timeout=5) as ws:
        await perform_signed_connect(
            ws,
            owner_id=owner_id,
            token=token,
            caps=caps,
            recv_timeout=recv_timeout,
            scopes=scopes,
        )

        req_id = str(uuid.uuid4())
        await ws.send(json.dumps({"type": "req", "id": req_id, "method": method, "params": params or {}}))

        # Skip event broadcasts until the matching response arrives.
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=rpc_timeout))
            if data.get("type") == "res" and data.get("id") == req_id:
                if not data.get("ok"):
                    raise RuntimeError(data.get("error", {}).get("message", f"{method} rejected"))
                return data.get("payload", {})
