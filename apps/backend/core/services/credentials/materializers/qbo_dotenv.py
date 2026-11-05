"""Materializer for Intuit's OFFICIAL QuickBooks MCP server (stdio, vendor build).

Writes the token store their client reads (``src/clients/quickbooks-client.ts``). The
``mcp_servers.quickbooks`` block is published by ``mcp_server`` /
``hermes_profiles.refresh_integrations`` from the registry + this store's PATH — never a
secret — so a reprovision rebuilds it from the registry alone.

Hermes dispatches this materializer through ``credentials.hermes_cli.project`` and lands the
result at ``$HOME/quickbooks/.env`` in every profile. The baked wrapper resets the client's
``QUICKBOOKS_TOKEN_STORE_PATH`` to that profile-local path before starting the server.

WHY THE CLIENT SECRET IS IN THIS FILE, when oauth_dance deliberately blanks it
------------------------------------------------------------------------------
``oauth_dance.exchange`` scrubs ``client_secret`` for every ``platform_app``
provider, so this materializer reads it from Secrets Manager directly rather than
from the credential. That is a NARROW, Intuit-specific exception. Do not copy it.

Intuit issues no public-client mode — its discovery document advertises only
``client_secret_post`` / ``client_secret_basic``, no ``none`` — so the secret is
required on every token call INCLUDING refresh. Their server refreshes itself on a
60-minute cycle, and Intuit's refresh tokens rotate with a rolling 100-day expiry
("Always store the latest refresh_token"), which means exactly one party may
refresh. Handing that job to the container is therefore forced, and a refresher
without the secret cannot work.

What makes the exposure acceptable HERE and nowhere else is redirect-URI pinning:
Intuit only delivers an authorization code to a URI pre-registered on the app, and
it exposes no app-level API. So client_id + client_secret ALONE cannot obtain any
owner's tokens — an attacker would need a code delivered to api*.thelightbulb.company,
which they do not control. The blast radius is brand impersonation on a consent
screen they cannot collect from, remediated by rotating the secret in the portal.

A Meta-shaped provider is NOT like this: there a leaked app secret enables
app-level Graph calls and long-lived-token exchange, which is exactly why the
blanket scrub in oauth_dance exists. Leave it alone.

Note the agent can read this file — it has ``exec``, and nothing inside a container
is hidden from a shell. ECS task-definition injection does not help: OpenClaw spawns
stdio servers through the MCP SDK's ``getDefaultEnvironment()``, which inherits only
[HOME, LOGNAME, PATH, SHELL, TERM, USER], so an injected var never reaches the child.
"""

from __future__ import annotations

import re

from core.services.credentials import platform_oauth_app_store, registry
from core.services.credentials.materializers.base import atomic_write, materializer_root, run_sync

_STORE_DIR = "quickbooks"
_STORE_FILE = ".env"
# KNOWN GAP (carried by deletion PR-5, not introduced by it): this is the ECS container's
# state dir. Nothing writes the store under it any more (see the module note), so the
# published block points at a file the microVM does not have. Fixing it means projecting the
# store into the profile and deriving this from the profile home.
_CONTAINER_STATE_DIR = "/home/node/.openclaw"

# Their client parses the store with dotenv. Rather than guess at dotenv's quoting
# and escape rules (single vs double quotes expand differently, and a mis-parse
# would silently produce a token that only LOOKS right), refuse anything outside
# the charset Intuit actually issues. Real client ids/secrets and tokens are
# URL-safe base64 with dots; realm ids are digits.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9._~+/=*-]+$")


def _store_path(owner_id: str):
    return materializer_root() / _STORE_DIR / _STORE_FILE


def container_store_path() -> str:
    """Path the SERVER reads, as seen from inside the container. Public because
    the reprovision rehydration builds the same block and must not re-derive it.
    Hermes starts the baked wrapper, which overrides this legacy absolute value
    with the profile-local ``$HOME/quickbooks/.env`` path."""
    return f"{_CONTAINER_STATE_DIR}/{_STORE_DIR}/{_STORE_FILE}"


def _intuit_environment() -> str:
    """Intuit's own env string. Development keys talk to sandbox; production keys
    to production, and the platform-app secret is already per-env
    (``lightbulb/<env>/integrations/platform-oauth/quickbooks``)."""
    return "production" if registry.current_env() == "prod" else "sandbox"


def _render(pairs: dict[str, str]) -> bytes:
    for key, value in pairs.items():
        if not value or not _SAFE_VALUE.match(value):
            raise ValueError(f"quickbooks: {key} is empty or has characters dotenv would mangle")
    return ("\n".join(f"{k}={v}" for k, v in pairs.items()) + "\n").encode()


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    """Seed the token store + publish the live mcp.servers.<key> block.

    Returns the realm id (the connected QuickBooks company) as the connection
    identity, mirroring how gog_cli returns the connected account's email."""
    p = registry.get_provider(provider_key)
    stdio = p.mcp_stdio
    if stdio is None:
        raise ValueError(f"{provider_key}: qbo_dotenv materializer requires an mcp_stdio spec")

    realm_id = (credential.get("realm_id") or "").strip()
    if not realm_id:
        # Without it every API path is unbuildable (/v3/company/{realmId}/...), so
        # fail the connect loudly rather than write a store that cannot work.
        raise ValueError("quickbooks: callback carried no realmId; cannot identify the company")

    if credential.get("custody") == "backend":
        # Spec 13: the backend refreshes; the server (brokered-token patch) reads the access token
        # the bridge writes into this store each turn. No refresh token, no client secret here.
        app = run_sync(platform_oauth_app_store.get(p.oauth.platform_app))
        if not app or not app.get("client_id"):
            raise ValueError(f"quickbooks: platform OAuth app {p.oauth.platform_app!r} is unseeded")
        body = _render(
            {
                "QUICKBOOKS_CLIENT_ID": app["client_id"],
                "QUICKBOOKS_REALM_ID": realm_id,
                "QUICKBOOKS_ENVIRONMENT": _intuit_environment(),
            }
        )
        atomic_write(_store_path(owner_id), body, mode=0o600)
        return realm_id

    app = run_sync(platform_oauth_app_store.get(p.oauth.platform_app))
    if not app or not app.get("client_secret"):
        raise ValueError(
            f"quickbooks: platform OAuth app {p.oauth.platform_app!r} is unseeded — "
            "run scripts/set_platform_oauth_app.py"
        )

    # Exactly the keys their client reads. The ACCESS token is deliberately absent:
    # it dies in 60 minutes and their client mints its own from the refresh token,
    # so persisting it would only leave a dead secret on disk.
    body = _render(
        {
            "QUICKBOOKS_CLIENT_ID": app["client_id"],
            "QUICKBOOKS_CLIENT_SECRET": app["client_secret"],
            "QUICKBOOKS_REFRESH_TOKEN": credential["refresh_token"],
            "QUICKBOOKS_REALM_ID": realm_id,
            "QUICKBOOKS_ENVIRONMENT": _intuit_environment(),
        }
    )
    # 0600, owned by node uid/gid 1000 in-container — same posture as every other
    # materialized credential file.
    atomic_write(_store_path(owner_id), body, mode=0o600)
    return realm_id


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    """Delete the token store. Idempotent: a missing file is a success, so a repeat
    disconnect never raises. The ``mcp_servers`` entry is dropped by the reconcile pass,
    which rebuilds the whole block from the connected set."""
    if registry.get_provider(provider_key).mcp_stdio is None:
        return
    _store_path(owner_id).unlink(missing_ok=True)
