"""Materializer for a remote, OAuth-protected MCP server (e.g. Attio's hosted MCP).

OpenClaw can't run its interactive ``openclaw mcp login`` CLI inside a headless
per-user container, so we reproduce that CLI's output from the backend after the
OAuth dance completes. OpenClaw delegates MCP OAuth to the MCP SDK's standard
``OAuthClientProvider``, which is backed by a plain JSON file. On every connect the
provider reads exactly two things from that file and AUTO-REFRESHES from them:

  - ``clientInformation`` — the registered OAuth client (OAuthClientInformationMixed:
    ``{client_id, client_secret?, ...DCR fields}``).
  - ``tokens`` — the OAuth tokens (OAuthTokens: ``{access_token, token_type,
    expires_in?, refresh_token?, scope?}``).

The login-flow transients (codeVerifier/state/discoveryState/lastAuthorizationUrl)
are NOT needed once final tokens exist, so writing ``{clientInformation, tokens}`` is
a bit-for-bit match to what the CLI's ``runMcpOAuthLogin`` produces.

The store path matches OpenClaw's ``oauthStorePath`` (mcp-oauth.ts):
``<stateDir>/mcp-oauth/<safeServerName>-<key16>.json`` where the container's
``resolveStateDir()`` is ``/home/node/.openclaw`` and the per-user EFS access point
mounts there — so from the backend the file lands under ``owner_efs_root(owner_id)``.

This materializer ALSO publishes the live ``mcp.servers.<key>`` block (transport +
url) so the runtime knows where to reach the remote server. That metadata rides on
the provider's ``mcp_remote`` field rather than the ``mcp_server`` wiring rail,
because this provider's declared rail is the materializer (exactly-one-of-three),
and the OAuth-token file — not an env var on a baked stdio process — is what carries
the credential.
"""

from __future__ import annotations

import hashlib
import json
import re

from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers import mcporter_vault
from core.services.credentials.materializers.base import atomic_write, owner_efs_root, run_sync

_MCP_OAUTH_DIR = "mcp-oauth"


# Our mcp.servers.<key> names are author-controlled and already lower-kebab/snake,
# so OpenClaw's sanitizeServerName is the identity on them. A minimal sanitizer
# (keep [a-z0-9_-], lowercase) reproduces that for our keys; we never register a
# server key outside that charset, so this is sufficient (and asserted at write).
_SAFE_KEY_RE = re.compile(r"[^a-z0-9_-]")


def _safe_server_name(server_name: str) -> str:
    return _SAFE_KEY_RE.sub("", server_name.lower())


def _store_key16(server_name: str, server_url: str) -> str:
    """OpenClaw's per-server store key: sha256(serverName + NUL + serverUrl)[:16].

    Mirrors mcp-oauth.ts exactly — serverName bytes, a literal NUL separator, then
    serverUrl bytes — so the file we write is the one the container reads."""
    h = hashlib.sha256()
    h.update(server_name.encode())
    h.update(b"\0")
    h.update(server_url.encode())
    return h.hexdigest()[:16]


def _store_path(owner_id: str, server_name: str, server_url: str):
    safe = _safe_server_name(server_name)
    return owner_efs_root(owner_id) / _MCP_OAUTH_DIR / f"{safe}-{_store_key16(server_name, server_url)}.json"


def _build_store(credential: dict, scopes: list[str]) -> dict:
    """The two-key MCP OAuth store the SDK's OAuthClientProvider reads + refreshes.

    Drops empty/None token fields so the file matches the CLI output (which omits
    them) rather than carrying explicit nulls."""
    tokens: dict = {
        "access_token": credential["access_token"],
        # token_type is fixed "Bearer" — the SDK sends it verbatim on every call.
        "token_type": "Bearer",
    }
    refresh_token = credential.get("refresh_token")
    if refresh_token:
        tokens["refresh_token"] = refresh_token
    expires_in = credential.get("expires_in")
    if expires_in:
        tokens["expires_in"] = int(expires_in)
    if scopes:
        tokens["scope"] = " ".join(scopes)
    return {"clientInformation": credential["client_information"], "tokens": tokens}


def write(owner_id: str, provider_key: str, credential: dict) -> str:
    """Seed the MCP SDK OAuth store + publish the live mcp.servers.<key> block.

    Returns the server display key as the connection identity (the natural handle
    for a remote MCP server, mirroring how gog_cli returns the account email)."""
    p = registry.get_provider(provider_key)
    remote = p.mcp_remote
    if remote is None:
        raise ValueError(f"{provider_key}: mcp_oauth materializer requires an mcp_remote spec")

    store = _build_store(credential, p.oauth.scopes if p.oauth else [])
    path = _store_path(owner_id, remote.key, remote.url)
    # 0600 file under a 0700 dir, owned by node uid/gid 1000 in-container — the
    # same posture as every other materialized credential file.
    atomic_write(path, json.dumps(store, indent=2).encode(), mode=0o600)

    # Publish the live remote-server block so the runtime knows where to connect.
    # The block (including auth: "oauth", which arms the SDK OAuth provider) is
    # built from the registry so it matches the reprovision rehydration exactly.
    # The materializer runs in a worker thread (apply_connection offloads it via
    # run_in_thread), so there is normally no running loop — _run_sync drives the
    # async config_patcher either way to the same mcp.servers.<key> path the
    # mcp_server rail uses, keeping behavior identical for the live config.
    run_sync(config_patcher.set_openclaw_config_path(owner_id, ["mcp", "servers", remote.key], remote.server_block()))

    # Second consumer of the SAME connect: seed mcporter's vault + config so lobster
    # workflows (which can't invoke MCP tools directly) can reach this server via
    # `mcporter call`. Reuses the OAuth tokens + DCR client we just wrote for the LLM.
    mcporter_vault.seed(owner_id, remote.key, remote.url, credential, p.oauth.scopes if p.oauth else [])
    return remote.key


def remove(owner_id: str, provider_key: str, credential: dict) -> None:
    """Delete the SDK OAuth store file AND drop the live mcp.servers.<key> block.

    Idempotent: a missing store file or absent server block is a success, so a
    repeat disconnect never raises."""
    p = registry.get_provider(provider_key)
    remote = p.mcp_remote
    if remote is None:
        return
    _store_path(owner_id, remote.key, remote.url).unlink(missing_ok=True)
    # Mirror the mcp_server rail's disconnect: remove the whole server entry rather
    # than blanking a leaf (a command/transport-less server is invalid config).
    run_sync(config_patcher.delete_openclaw_config_path(owner_id, ["mcp", "servers", remote.key]))
    # Tear down the mcporter side of the same connect.
    mcporter_vault.unseed(owner_id, remote.key, remote.url)
