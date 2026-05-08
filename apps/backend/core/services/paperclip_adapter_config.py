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

OPENCLAW_GATEWAY_TYPE: Final[str] = "openclaw_gateway"

# Matches: wss://ws.isol8.co (prod), wss://ws-{env}.isol8.co (dev/staging),
# ws://localhost:{port} (local). Anchored. No path component allowed.
_GATEWAY_URL_RE: Final[re.Pattern[str]] = re.compile(r"\A(?:wss://ws(?:-[a-z]+)?\.isol8\.co|ws://localhost:[0-9]+)\Z")


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
) -> dict:
    """Return the canonical adapterConfig dict for openclaw_gateway.

    The shape mirrors the existing production payload at
    ``paperclip_provisioning.py:256-261`` so seeded agents and
    user-created agents have identical wire format.
    """
    validate_gateway_url(gateway_url)
    if not service_token or not isinstance(service_token, str):
        raise AdapterConfigError("service_token is empty or non-string")
    if not user_id or not isinstance(user_id, str):
        raise AdapterConfigError("user_id is empty or non-string")

    return {
        "url": gateway_url,
        "authToken": service_token,
        "sessionKeyStrategy": "issue",
        "sessionKey": user_id,
    }
