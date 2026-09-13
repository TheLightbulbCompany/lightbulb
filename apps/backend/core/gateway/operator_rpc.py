"""Shared OpenClaw signed operator-connect helper.

OpenClaw 4.5 replaced the pre-4.5 "token == admin" model with scoped auth: a
connect request that presents only ``auth.token`` (no signed device identity)
gets its self-declared scopes silently cleared by the server, leaving a
zero-permission connection. To actually receive ``operator.admin`` (needed for
``config.*``, ``update.*``, ``agents.*``, ``skills.*``, etc.) the backend must
sign the connect challenge with the per-container Ed25519 operator device.

That signed handshake used to be copy-pasted into four places (the connection
pool, the recovery endpoint, the credential-revoke service, and the
gateway-restart endpoint). ``perform_signed_connect`` is the single canonical
implementation: it runs the challenge → sign → hello-ok handshake on an
already-open websocket. Everything but the operator-device pairing probe now
reaches the gateway over HTTP via ``core.services.container_rpc``; the probe
still needs a real signed device connect (sub-project D replaces it).

``GATEWAY_PORT`` lives here (not imported from ``core.containers.ecs_manager``)
to avoid a gateway → containers import cycle.
"""

import asyncio
import json
import uuid


GATEWAY_PORT = 18789  # OpenClaw gateway port (avoid circular import with containers/)

# Default per-recv timeout (seconds) for the short handshake/RPC exchanges.
_DEFAULT_TIMEOUT = 10

# OpenClaw's canonical "service or required backend is temporarily
# unavailable" code (``ErrorCodes.UNAVAILABLE`` in its gateway-protocol
# package). Checked ALONGSIDE the error's own ``retryable`` flag rather than
# instead of it: the flag is the semantic signal, this code is the belt for a
# gateway version that sends the code without the flag.
_UNAVAILABLE_CODE = "UNAVAILABLE"


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


class GatewayConnectUnavailableError(RuntimeError):
    """Connect rejected as TEMPORARILY unavailable — the gateway process is up
    enough to answer the handshake but not ready to admit a session.

    The canonical case is OpenClaw's own startup window: ``connect-admission``
    answers ``errorShape(ErrorCodes.UNAVAILABLE, "gateway starting; retry
    shortly", {retryable: true, retryAfterMs})`` while the container boots.
    Nothing about that rejection is permanent — the very next connect succeeds.

    It used to arrive as a bare ``RuntimeError`` and could be mistaken for an
    auth rejection, turning a short startup delay into a terminal onboarding
    failure.

    Discriminated on the STRUCTURED signal the gateway sends (``retryable`` /
    the ``UNAVAILABLE`` code), never on the message text, which is free-form
    and varies per call site. Subclasses RuntimeError so pre-existing broad
    handlers keep working; callers that care match it by TYPE.
    """

    def __init__(self, message: str, *, code: str | None = None, retry_after_ms: int | None = None):
        super().__init__(message)
        self.code = code
        self.retry_after_ms = retry_after_ms


def _retry_after_ms(error: dict) -> int | None:
    """The gateway's own back-off hint, if it sent one.

    ``retryAfterMs`` sits directly on the error object or nested under
    ``error.data``, depending on the gateway version.
    """
    retry_after = error.get("retryAfterMs")
    if retry_after is None:
        data = error.get("data")
        if isinstance(data, dict):
            retry_after = data.get("retryAfterMs")
    return int(retry_after) if isinstance(retry_after, (int, float)) else None


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
    trusted_broker_profile_id: str | None = None,
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
        # KMS Decrypt is a SYNCHRONOUS boto3 call. This handshake runs on the
        # connect hot path (held under the pool's single lock), so calling it
        # inline blocks the whole event loop for the round trip — one slow KMS
        # response stalls every user on this backend task. Offload to a worker
        # thread. (GW-2a.)
        seed_bytes = await asyncio.to_thread(
            kms_secrets.decrypt_bytes,
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
        platform="linux" if trusted_broker_profile_id is not None else None,
        trusted_broker_profile_id=trusted_broker_profile_id,
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
        "auth": {
            "token": token,
            **({"trustedBrokerProfileId": trusted_broker_profile_id} if trusted_broker_profile_id is not None else {}),
        },
        "device": device,
    }
    if caps:
        params["caps"] = list(caps)
    request_id = str(uuid.uuid4())
    await ws.send(json.dumps({"type": "req", "id": request_id, "method": "connect", "params": params}))

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
            # Checked BEFORE the retryable branch below on purpose: an
            # AUTH_RATE_LIMITED rejection may well set ``retryable``, but it
            # still counts toward the gateway's 10-in-60s lockout, so it must
            # stay an auth error rather than become something a caller retries.
            raise GatewayConnectAuthError(
                f"Gateway connect failed: {message}",
                code=code,
                retry_after_ms=_retry_after_ms(error),
            )
        if error.get("retryable") is True or code == _UNAVAILABLE_CODE:
            raise GatewayConnectUnavailableError(
                f"Gateway connect failed: {message}",
                code=code,
                retry_after_ms=_retry_after_ms(error),
            )
        raise RuntimeError(f"Gateway connect failed: {message}")
    if trusted_broker_profile_id is not None:
        hello = resp.get("payload")
        auth = hello.get("auth") if isinstance(hello, dict) else None
        if not (
            resp.get("type") == "res"
            and resp.get("id") == request_id
            and resp.get("ok") is True
            and isinstance(hello, dict)
            and hello.get("type") == "hello-ok"
            and hello.get("protocol") == 4
            and isinstance(auth, dict)
            and auth.get("role") == "operator"
            and set(auth.get("scopes", [])) == set(scopes)
            and not {"deviceToken", "deviceTokens", "bootstrapToken"} & auth.keys()
            and not {"deviceToken", "deviceTokens", "bootstrapToken"} & hello.keys()
        ):
            raise RuntimeError("Gateway trusted-profile handshake proof is invalid")
    return resp
