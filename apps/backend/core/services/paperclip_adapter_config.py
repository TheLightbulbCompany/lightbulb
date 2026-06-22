"""Server-side synthesis of openclaw_gateway adapter config for Paperclip.

Spec §5 security invariant: the BFF NEVER accepts ``adapterType``,
``adapterConfig``, or any nested URL/header field from the client.
Every agent-mutating BFF endpoint calls ``synthesize_openclaw_adapter``
to assemble the upstream body. The URL is allowlisted against the
env-derived gateway hostnames; an out-of-allowlist URL is an operator
bug (the URL is read from our own infrastructure), not user input,
so we raise rather than 4xx.

Field shape per ``paperclip/packages/adapters/openclaw-gateway/src/index.ts:21``:
{ url, authToken, sessionKeyStrategy, sessionKey }. Note ``authToken``,
not ``token``.

Adapter type is ``"openclaw_gateway"`` with an underscore — canonical
per ``paperclip/packages/shared/src/constants.ts:40``.

Session key strategy is ``"issue"`` so each Paperclip issue assigned
to an agent gets its own OpenClaw session
(``agent:<agent_id>:paperclip:issue:<issue_id>``). This isolates
transcripts across unrelated issues; long-term knowledge still
persists across sessions via OpenClaw's memory plugin (MEMORY.md).
The ``sessionKey`` field is the fallback for wakes without an issueId
(manual ``Run Heartbeat``, sibling-agent wake, etc.) and is set to
the agent creator's user_id so manual wakes route to a stable
per-creator session.
"""

from __future__ import annotations

import re
from typing import Final

from core.crypto.operator_device import generate_operator_device, private_key_pem

OPENCLAW_GATEWAY_TYPE: Final[str] = "openclaw_gateway"

# clientId the synthesized adapter URL advertises so the WS gateway routes the
# connection to the operator relay at $connect (the connect Lambda forwards
# `?clientId=` as the `x-client-id` header ws_connect classifies on). Distinct
# from the desktop node-host client ids.
PAPERCLIP_OPERATOR_CLIENT_ID: Final[str] = "paperclip-operator"

# Matches: wss://ws.isol8.co (prod), wss://ws-{env}.isol8.co (dev/staging),
# ws://localhost:{port} (local). Anchored. No path component allowed.
_GATEWAY_URL_RE: Final[re.Pattern[str]] = re.compile(r"\A(?:wss://ws(?:-[a-z]+)?\.isol8\.co|ws://localhost:[0-9]+)\Z")


def claimed_api_key_path(agent_id: str) -> str:
    """Per-agent path to the Paperclip claimed-key file the agent reads at wake.

    Each agent's own workspace is its cwd (``~/.openclaw/workspaces/{agentId}/``,
    PLURAL — the singular ``~/.openclaw/workspace`` is container-global and would
    collide across agents). Isol8 pre-stages the per-agent identity file there
    (``friday_identity.pre_stage_paperclip_identities``), and the seed prompt
    instructs the agent to load it from its own cwd. We also surface it as the
    adapter's ``claimedApiKeyPath`` so Paperclip's native wake flow points at the
    same per-agent file rather than the colliding default.
    """
    return f"~/.openclaw/workspaces/{agent_id}/paperclip-claimed-api-key.json"


class AdapterConfigError(Exception):
    """Raised when adapter-config inputs fail validation."""


def validate_gateway_url(url: str | None) -> None:
    """Raise AdapterConfigError unless ``url`` matches the allowlist."""
    if not url or not isinstance(url, str):
        raise AdapterConfigError(f"gateway URL is empty or non-string: {url!r}")
    if not _GATEWAY_URL_RE.match(url):
        raise AdapterConfigError(f"gateway URL not in allowlist: {url!r}")


def synthesize_openclaw_adapter(
    *,
    gateway_url: str,
    service_token: str,
    user_id: str,
    device_private_key_pem: str | None = None,
    agent_id: str | None = None,
) -> dict:
    """Return the canonical adapterConfig dict for openclaw_gateway.

    The shape mirrors the existing production payload at
    ``paperclip_provisioning.py:256-261`` so seeded agents and
    user-created agents have identical wire format.

    ``device_private_key_pem``: Isol8 mints + owns the per-agent ed25519 device
    keypair so the deviceId is stable and can be pre-registered in the
    container's ``paired.json`` before the first connect (a remote operator
    device that isn't pre-paired is rejected NOT_PAIRED). Pass the agent's
    EXISTING PEM on update so the deviceId — and its seeded paired.json entry —
    stay stable across edits; a fresh keypair is minted only when absent (create).

    ``agent_id``: when supplied, the returned config carries a per-agent
    ``claimedApiKeyPath`` pointing at the agent's own workspace identity file.
    Create-time callers don't yet know the id (Paperclip assigns it), so they
    omit it; the per-agent path is applied post-create at the pairing seam
    (``register_paperclip_agent_devices`` / ``ensure_operator_device_paired``)
    once the id is known. Omitted ⇒ ``claimedApiKeyPath`` is left off so the
    create-time wire shape is unchanged.
    """
    validate_gateway_url(gateway_url)
    if not service_token or not isinstance(service_token, str):
        raise AdapterConfigError("service_token is empty or non-string")
    if not user_id or not isinstance(user_id, str):
        raise AdapterConfigError("user_id is empty or non-string")

    # The Isol8 WS API Gateway authorizer reads the auth token ONLY from
    # the `?token=` query string (identitySource
    # `route.request.querystring.token`). The upstream Paperclip
    # openclaw_gateway adapter sends `authToken` as an Authorization header,
    # which API Gateway ignores, so the handshake 401s before the authorizer
    # Lambda runs. Embedding the token in the URL query string is where the
    # authorizer looks — the same mechanism the browser web-chat client uses.
    # `authToken` is kept for adapter compatibility; the duplicate header is
    # harmless because API Gateway authorizes on the query string. The token
    # is URL-safe (JWT base64url uses only [A-Za-z0-9-_.]), so no escaping is
    # needed. The full adapterConfig (incl. this URL) is stripped by
    # `_redact_agent` before any agent record reaches the browser.
    #
    # `clientId` routes the connection to the operator relay at $connect — the WS
    # connect Lambda forwards `?clientId=` as `x-client-id`. It is appended after
    # `validate_gateway_url` (which checks the bare host), so the allowlist regex
    # is untouched.
    device_pem = device_private_key_pem or private_key_pem(generate_operator_device())
    cfg = {
        "url": f"{gateway_url}?token={service_token}&clientId={PAPERCLIP_OPERATOR_CLIENT_ID}",
        "authToken": service_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": user_id,
        "devicePrivateKeyPem": device_pem,
    }
    if agent_id:
        cfg["claimedApiKeyPath"] = claimed_api_key_path(agent_id)
    return cfg
