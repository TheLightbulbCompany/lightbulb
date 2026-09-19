"""Seed mcporter's credential vault + server config so lobster workflows can call a
hosted-OAuth MCP server (e.g. Attio) deterministically via ``mcporter call``.

Lobster pipeline steps cannot invoke MCP tools — those reach only the agent LLM's
tool loop — so the workflows shell out to the baked ``mcporter`` CLI instead.
mcporter keeps its OWN credential store, independent of OpenClaw's MCP-SDK store, so
the mcp_oauth materializer writes BOTH from the same OAuth connect: the OpenClaw
store (for the LLM) and the two mcporter files here (for the workflows).

mcporter splits its files across two locations: it reads the server CONFIG from
``~/.mcporter/mcporter.json`` (the container symlinks ``~/.mcporter`` ->
``<stateDir>/mcporter``, so we write ``mcporter.json`` under ``materializer_root/mcporter``),
but it reads/writes the OAuth VAULT from ``$XDG_DATA_HOME/mcporter/credentials.json``.
The container sets ``XDG_DATA_HOME`` to ``<stateDir>/.local/share`` (on EFS, so creds
survive restarts), so we write ``credentials.json`` under
``materializer_root/.local/share/mcporter``. Writing the vault under ``~/.mcporter`` does
NOT work — mcporter ignores it (``mcporter list`` reports "auth required" / 401 "No token
provided"); its own ``mcporter vault set`` writes to the XDG path.
The vault entry carries ``{tokens, clientInfo}`` — the SAME MCP-SDK snake_case shapes
our materializer already builds — and, given a ``refresh_token`` +
``clientInfo.client_id``, mcporter auto-refreshes the access token on every call and
rewrites the vault (verified against mcporter 0.12 ``oauth-persistence``). Omitting
clientInfo silently disables refresh, so both MUST be seeded.

Both files are SHARED across all hosted-MCP providers (one vault, one ``mcpServers``
map), so a write MERGES this server's entry under an ``fcntl`` lock rather than
overwriting a sibling provider's entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from core.services.credentials.materializers.base import NODE_GID, NODE_UID, materializer_root
from core.dynamodb import utc_now_iso

_MCPORTER_DIR = "mcporter"


def _vault_path(owner_id: str) -> Path:
    # mcporter reads/writes its OAuth vault from $XDG_DATA_HOME/mcporter, NOT
    # ~/.mcporter. The container sets XDG_DATA_HOME=<stateDir>/.local/share (on EFS),
    # so the vault lives under materializer_root/.local/share/mcporter.
    return materializer_root() / ".local" / "share" / _MCPORTER_DIR / "credentials.json"


def _config_path(owner_id: str) -> Path:
    # Server config, by contrast, is read from ~/.mcporter (symlinked to
    # <stateDir>/mcporter by the container startup command).
    return materializer_root() / _MCPORTER_DIR / "mcporter.json"


def _mcporter_url(server_url: str) -> str:
    """The URL as mcporter sees it: it parses the configured URL with WHATWG
    ``new URL()`` before hashing, which serializes a bare origin with a trailing
    slash (``https://mcp.stripe.com`` -> ``https://mcp.stripe.com/``) and lowercases
    scheme/host. Hash the raw string instead and a bare-origin server gets a vault
    key mcporter never looks up — it reports "auth required" and starts its own
    interactive login, which can't complete headless."""
    parts = urlsplit(server_url)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    # WHATWG also drops the scheme's default port from the serialization.
    if (scheme, parts.port) in (("https", 443), ("http", 80)):
        netloc = netloc.rsplit(":", 1)[0]
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, parts.fragment))


def _vault_key(server_key: str, server_url: str) -> str:
    """mcporter's per-entry key: ``<name>|<sha256(json)[:16]>`` where json is
    ``{"name","url","command":null}`` for an HTTP server. The JSON byte-matches
    mcporter's ``JSON.stringify`` (insertion order, no spaces, null command) so the
    entry we seed is the exact one a ``mcporter call`` resolves from the config."""
    payload = json.dumps({"name": server_key, "url": _mcporter_url(server_url), "command": None}, separators=(",", ":"))
    return f"{server_key}|{hashlib.sha256(payload.encode()).hexdigest()[:16]}"


def _tokens(credential: dict, scopes: list[str]) -> dict:
    """MCP-SDK OAuthTokens shape (snake_case). token_type is required and fixed
    Bearer; empty/None fields are dropped to match a freshly-saved vault."""
    tokens: dict = {"access_token": credential["access_token"], "token_type": "Bearer"}
    if credential.get("refresh_token"):
        tokens["refresh_token"] = credential["refresh_token"]
    if credential.get("expires_in"):
        tokens["expires_in"] = int(credential["expires_in"])
    if scopes:
        tokens["scope"] = " ".join(scopes)
    return tokens


def _locked_update(path: Path, default: dict, mutate) -> None:
    """fcntl-locked read-modify-write of a JSON file on EFS. Creates from ``default``
    when absent or unparseable. 0600, chowned to the in-container node uid/gid."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.lockf(fd, fcntl.LOCK_EX)
        raw = os.read(fd, 1 << 24).decode()
        try:
            data = json.loads(raw) if raw.strip() else json.loads(json.dumps(default))
        except json.JSONDecodeError:
            data = json.loads(json.dumps(default))
        mutate(data)
        os.lseek(fd, 0, 0)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps(data, indent=2).encode())
        if os.geteuid() == 0:  # in-container only; tests run non-root
            os.fchown(fd, NODE_UID, NODE_GID)
    finally:
        fcntl.lockf(fd, fcntl.LOCK_UN)
        os.close(fd)
    if os.geteuid() == 0:
        os.chown(str(path.parent), NODE_UID, NODE_GID)


def seed(owner_id: str, server_key: str, server_url: str, credential: dict, scopes: list[str]) -> None:
    """Add (or replace) this server's mcporter config + vault entry, idempotently."""
    key = _vault_key(server_key, server_url)
    entry = {
        "serverName": server_key,
        "serverUrl": _mcporter_url(server_url),
        "tokens": _tokens(credential, scopes),
        "clientInfo": credential["client_information"],
        "updatedAt": utc_now_iso(),
    }

    def _add_vault(d: dict) -> None:
        d.setdefault("version", 1)
        d.setdefault("entries", {})[key] = entry

    def _add_config(d: dict) -> None:
        d.setdefault("mcpServers", {})[server_key] = {"url": server_url, "auth": "oauth"}

    _locked_update(_vault_path(owner_id), {"version": 1, "entries": {}}, _add_vault)
    _locked_update(_config_path(owner_id), {"mcpServers": {}}, _add_config)


def unseed(owner_id: str, server_key: str, server_url: str) -> None:
    """Drop this server's vault entry + config block. Missing files/keys are a
    no-op so a repeat disconnect never raises."""
    key = _vault_key(server_key, server_url)
    if _vault_path(owner_id).exists():
        _locked_update(
            _vault_path(owner_id), {"version": 1, "entries": {}}, lambda d: d.get("entries", {}).pop(key, None)
        )
    if _config_path(owner_id).exists():
        _locked_update(
            _config_path(owner_id), {"mcpServers": {}}, lambda d: d.get("mcpServers", {}).pop(server_key, None)
        )
