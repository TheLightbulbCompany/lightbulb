"""Seed mcporter's credential vault + server config so lobster workflows can call a
hosted-OAuth MCP server (e.g. Attio) deterministically via ``mcporter call``.

Lobster pipeline steps cannot invoke MCP tools — those reach only the agent LLM's
tool loop — so the workflows shell out to the baked ``mcporter`` CLI instead.
mcporter keeps its OWN credential store, independent of OpenClaw's MCP-SDK store, so
the mcp_oauth materializer writes BOTH from the same OAuth connect: the OpenClaw
store (for the LLM) and the two mcporter files here (for the workflows).

mcporter reads ``~/.mcporter/{mcporter.json,credentials.json}``. The container startup
command (container-stack.ts) symlinks ``~/.mcporter`` -> ``<stateDir>/mcporter`` — the
same EFS-relocation idiom as the npm cache — so the files we write here under
``owner_efs_root/mcporter`` are exactly what mcporter reads, and they survive restarts.
The vault entry carries ``{tokens, clientInfo}`` — the SAME MCP-SDK snake_case shapes
our materializer already builds — and, given a ``refresh_token`` +
``clientInfo.client_id``, mcporter auto-refreshes the access token on every call and
rewrites the vault (verified against mcporter 0.12 ``oauth-persistence``). Omitting
clientInfo silently disables refresh, so both MUST be seeded.

Both files are SHARED across all hosted-MCP providers (one vault, one ``mcpServers``
map), so a write MERGES this server's entry under an ``fcntl`` lock (NFS-safe, like
config_patcher) rather than overwriting a sibling provider's entry.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from core.services.credentials.materializers.base import NODE_GID, NODE_UID, owner_efs_root

_MCPORTER_DIR = "mcporter"


def _vault_path(owner_id: str) -> Path:
    return owner_efs_root(owner_id) / _MCPORTER_DIR / "credentials.json"


def _config_path(owner_id: str) -> Path:
    return owner_efs_root(owner_id) / _MCPORTER_DIR / "mcporter.json"


def _vault_key(server_key: str, server_url: str) -> str:
    """mcporter's per-entry key: ``<name>|<sha256(json)[:16]>`` where json is
    ``{"name","url","command":null}`` for an HTTP server. The JSON byte-matches
    mcporter's ``JSON.stringify`` (insertion order, no spaces, null command) so the
    entry we seed is the exact one a ``mcporter call`` resolves from the config."""
    payload = json.dumps({"name": server_key, "url": server_url, "command": None}, separators=(",", ":"))
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
        "serverUrl": server_url,
        "tokens": _tokens(credential, scopes),
        "clientInfo": credential["client_information"],
        "updatedAt": datetime.now(timezone.utc).isoformat(),
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
