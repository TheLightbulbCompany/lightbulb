"""Server-side synthesis of openclaw_gateway adapter config for Paperclip.

Spec §5 security invariant: the BFF NEVER accepts ``adapterType``,
``adapterConfig``, or any nested URL/header field from the client.
Every agent-mutating BFF endpoint calls ``synthesize_openclaw_adapter``
to assemble the upstream body. The URL is allowlisted against the
env-derived gateway hostnames; an out-of-allowlist URL is an operator
bug (the URL is read from our own infrastructure), not user input,
so we raise rather than 4xx.

Field shape per ``paperclip/packages/adapters/openclaw-gateway/src/index.ts``:
{ url, authToken, sessionKeyStrategy, sessionKey }. Note ``authToken``,
not ``token``.

Adapter type is ``"openclaw_gateway"`` with an underscore — canonical
per ``paperclip/packages/shared/src/constants.ts``.

Session key strategy is ``"issue"`` so each Paperclip issue assigned
to an agent gets its own OpenClaw session
(``agent:<agent_id>:paperclip:issue:<issue_id>``). This isolates
transcripts across unrelated issues; long-term knowledge still
persists across sessions via OpenClaw's memory plugin (MEMORY.md).
The ``sessionKey`` field is the fallback for wakes without an issueId
(manual ``Run Heartbeat``, sibling-agent wake, etc.) and is
``paperclip:<user_id>`` so manual wakes route to a stable per-creator
session that is still unambiguously a Paperclip one.

The ``paperclip:`` prefix on that fallback is load-bearing, not
cosmetic. Once ``agentId`` is set the adapter prefixes the fallback with
``agent:<agentId>:`` (openclaw-gateway execute.ts), so a bare
``user_id`` fallback would resolve to ``agent:<agentId>:<user_id>`` —
byte-identical to what ``build_chat_session_key(agent_id, user_id,
session_id=None)`` (routers/websocket_chat.py) produces for the owner's
"General" /chat session. Every heartbeat/manual wake would then land IN
the owner's real chat conversation: appended to that transcript, loading
its whole history as context, and streamed live into their open chat
window (``_parse_session_key`` would classify it ``source="webchat"``,
so ``is_isolated_session`` is False). Keeping ``paperclip`` at parts[2]
makes ``_parse_session_key`` return ``source="paperclip"`` — the shape
its own docstring already documents as the bare fallback key.
"""

from __future__ import annotations

import re
from typing import Final

from core.config import apex_domain, settings, url_host
from core.crypto.operator_device import generate_operator_device, private_key_pem

OPENCLAW_GATEWAY_TYPE: Final[str] = "openclaw_gateway"

# clientId the synthesized adapter URL advertises so the WS gateway routes the
# connection to the operator relay at $connect (the connect Lambda forwards
# `?clientId=` as the `x-client-id` header ws_connect classifies on). Distinct
# from the desktop node-host client ids.
PAPERCLIP_OPERATOR_CLIENT_ID: Final[str] = "paperclip-operator"


def build_gateway_url_re(ws_gateway_url: str) -> re.Pattern[str]:
    """Allowlist pattern for an openclaw_gateway adapter URL.

    Matches: wss://ws.<apex> (prod), wss://ws-{env}.<apex> (dev/staging),
    ws://localhost:{port} (local). Anchored. No path component allowed.

    <apex> is the registrable domain of ``ws_gateway_url`` — the same value CDK
    hands the WS gateway — so a domain move needs no code change here. The check
    is fail-CLOSED (``validate_gateway_url`` RAISES, and every agent
    create/update path calls it), so a ``ws_gateway_url`` with no host drops the
    hosted branch entirely rather than degrading into a looser pattern.
    """
    apex = apex_domain(url_host(ws_gateway_url))
    hosted = rf"wss://ws(?:-[a-z]+)?\.{re.escape(apex)}|" if apex else ""
    return re.compile(rf"\A(?:{hosted}ws://localhost:[0-9]+)\Z")


_GATEWAY_URL_RE: Final[re.Pattern[str]] = build_gateway_url_re(settings.WS_GATEWAY_URL)

# Per-run agent timeout (seconds) + the gateway's wait for that run (ms). The
# openclaw-gateway adapter defaults BOTH to 120s (build-config.ts —
# `timeoutSec=120`, `waitTimeoutMs=120000`), a value tuned for short, single-step
# heartbeat runs. Our agents do substantive single-run work that routinely exceeds
# 2 min, so at 120s the gateway stamps a still-working run `timed_out` and
# dispatches a retry while the original is mid-run; that retry queues on the
# per-issue session lane and times out too — a cascade of `timed_out` runs even
# though the work commits. We pin 10 min — the full API Gateway WebSocket idle
# ceiling — to give each single wait the maximum runway. NOTE: 10 min sits AT the
# idle ceiling, so a run still silent at ~10 min drops the socket (surfacing as a
# reconnect/retry) rather than a clean `timeout`; that churn is the accepted
# tradeoff for the extra runway. Work that legitimately needs longer than one wait
# is a checkpointing problem, not a bigger-timeout one.
AGENT_RUN_TIMEOUT_SEC: Final[int] = 600
AGENT_RUN_WAIT_TIMEOUT_MS: Final[int] = 600_000


def claimed_api_key_path(agent_id: str) -> str:
    """Per-agent path to the Paperclip claimed-key file the agent reads at wake.

    Each agent's own workspace is its cwd (``~/.openclaw/workspace-{agentId}/``,
    upstream's per-agent convention — the bare ``~/.openclaw/workspace`` is the
    default-agent dir and would collide across agents). Lightbulb AI pre-stages the per-agent identity file there
    (``friday_identity.pre_stage_paperclip_identities``). We surface it as the
    adapter's ``claimedApiKeyPath`` so Paperclip's native wake flow points at the
    same per-agent file rather than the colliding default.

    NOTE: the AGENT no longer reads this file — the ``paperclip-exec-creds``
    plugin injects ``PAPERCLIP_*`` into every exec child, and the seeded
    playbook now forbids reading or printing the key (it used to instruct the
    read, and the model echoed a live token into its visible reasoning). The
    path still matters because Paperclip's own wake text is generated from
    ``claimedApiKeyPath`` and still tells the agent to load the key from it —
    a fork-side instruction we cannot remove from here, which is why the
    playbook explicitly tells the agent to ignore it.
    """
    return f"~/.openclaw/workspace-{agent_id}/paperclip-claimed-api-key.json"


def desired_agent_adapter_fields(*, agent_id: str, gateway_token: str) -> dict:
    """The per-agent ``adapterConfig`` fields the pairing seams must keep current.

    ONE definition, consumed by both seams
    (``register_paperclip_agent_devices`` / ``ensure_operator_device_paired``)
    for BOTH their drift check and their patch body. They previously hand-rolled
    the same dict twice, so the drift condition and the written body could
    disagree, and adding a field meant remembering four places.

    - ``authToken`` — the container's gateway token. The adapter's signed connect
      computes its signature over ``auth.token``, so this must match the
      container or the connect is rejected. (The URL keeps ``?token=<service
      JWT>`` for the API Gateway authorizer; different token, different job.)
    - ``agentId`` — routes the run to THIS agent. Unset, the ``chat.send``
      carries no agent and OpenClaw falls back to the ``default: true`` agent —
      the ``role="ceo"`` one (``containers/config.py`` ``_build_agents_list``),
      i.e. Friday — so every other agent's run executes AS Friday, in Friday's
      workspace, reading Friday's claimed key.
    - ``claimedApiKeyPath`` — the agent's own per-agent identity file; the
      singular ``~/.openclaw/workspace`` default collides across agents.
    """
    return {
        "authToken": gateway_token,
        "agentId": agent_id,
        "claimedApiKeyPath": claimed_api_key_path(agent_id),
    }


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

    ``device_private_key_pem``: Lightbulb AI mints + owns the per-agent ed25519 device
    keypair so the deviceId is stable and can be pre-registered in the
    container's ``paired.json`` before the first connect (a remote operator
    device that isn't pre-paired is rejected NOT_PAIRED). Pass the agent's
    EXISTING PEM on update so the deviceId — and its seeded paired.json entry —
    stay stable across edits; a fresh keypair is minted only when absent (create).

    ``agent_id``: when supplied, the returned config carries the per-agent
    ``agentId`` + ``claimedApiKeyPath`` (see ``desired_agent_adapter_fields``
    for what each does). Create-time callers don't yet know the id — Paperclip
    assigns it — so they omit it and the pairing seams apply both post-create.
    ``desired_agent_adapter_fields`` is the authority for the seams; this
    branch only keeps the two shapes consistent for callers that DO know the id
    up front. Omitted ⇒ both are left off, unchanged create-time wire shape.
    """
    validate_gateway_url(gateway_url)
    if not service_token or not isinstance(service_token, str):
        raise AdapterConfigError("service_token is empty or non-string")
    if not user_id or not isinstance(user_id, str):
        raise AdapterConfigError("user_id is empty or non-string")

    # The Lightbulb AI WS API Gateway authorizer reads the auth token ONLY from
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
        # `paperclip:` prefix is REQUIRED — see the session-key note in the
        # module docstring. Bare `user_id` collides with the owner's /chat
        # "General" session key once `agentId` is set.
        "sessionKey": f"paperclip:{user_id}",
        "devicePrivateKeyPem": device_pem,
        # Raise the run + wait timeout off the adapter's 120s default; our agents'
        # runs routinely exceed 2 min and 120s mislabels them `timed_out` and
        # triggers a retry/lane-contention cascade. See the constants above.
        "timeoutSec": AGENT_RUN_TIMEOUT_SEC,
        "waitTimeoutMs": AGENT_RUN_WAIT_TIMEOUT_MS,
    }
    # `paperclipApiUrl` is the host the agent's bundled `paperclip` skill curls at
    # wake (`$PAPERCLIP_API_URL`). The Paperclip server's per-agent override
    # (openclaw-gateway execute.ts: resolvePaperclipApiUrlOverride) takes precedence
    # over its default — which is `origin(PAPERCLIP_PUBLIC_URL)` (Better Auth's
    # public/auth origin, NOT reachable from inside the VPC). Without this override
    # the per-user container curls that public origin and every heartbeat run blocks
    # on DNS. Pin it to the in-VPC Cloud Map host the containers can actually reach.
    # This is what lets PAPERCLIP_PUBLIC_URL stay the (https) auth origin.
    if settings.PAPERCLIP_INTERNAL_URL:
        cfg["paperclipApiUrl"] = settings.PAPERCLIP_INTERNAL_URL
    if agent_id:
        cfg["agentId"] = agent_id
        cfg["claimedApiKeyPath"] = claimed_api_key_path(agent_id)
    return cfg
