"""Wiring engine: take a fetched credential for (owner, provider) and deliver it
into the owner's container via the rail the registry specifies."""

from __future__ import annotations

from typing import Any

from core.dynamodb import run_in_thread
from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers import MATERIALIZERS


def _resolve_source(source: str, credential: dict[str, Any], raw_input: dict[str, Any]) -> Any:
    # "literal:true" -> "true" (a static env value, no credential dependency);
    # "input.token" -> raw_input["token"]; "credentials.access_token" -> credential["access_token"]; dotted = nested.
    if source.startswith("literal:"):
        # Split once so a value may itself contain a colon (e.g. literal:a:b).
        return source.split(":", 1)[1]
    root, *rest = source.split(".")
    cur: Any = raw_input if root == "input" else credential
    for part in rest:
        cur = cur[part]
    return cur


async def apply_connection(owner_id: str, provider_key: str, credential: dict[str, Any]) -> Any:
    """credential is the resolved token payload ({"token": ...} for paste, or
    {"access_token","refresh_token",...} for OAuth). Async because config_patcher is async.

    Returns whatever the wiring produced as an identity for the connection — the
    materializer rail surfaces its return (gog_cli returns the connected Google
    account's email). The config-delivery rails have nothing to surface and
    return None, which a generic caller can ignore."""
    p = registry.get_provider(provider_key)
    w = p.wiring
    if w.openclaw_paths:
        for op in w.openclaw_paths:
            value = _resolve_source(op.source, credential, credential)
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), value)
        return None
    if w.materializer:
        # Materializers are sync; gog_cli blocks on subprocess + HTTP, so offload to a
        # thread to avoid stalling the event loop.
        return await run_in_thread(MATERIALIZERS[w.materializer], owner_id, provider_key, credential)
    if w.mcp_server:
        from core.services.credentials.mcp_server import build_mcp_server_block

        block = build_mcp_server_block(provider_key, credential)
        # Replace the whole server subtree — the integration OWNS mcp.servers.<key>,
        # so a stale/scrubbed/user value for this key is overwritten.
        await config_patcher.set_openclaw_config_path(owner_id, ["mcp", "servers", w.mcp_server.key], block)
        return None
    raise RuntimeError(f"{provider_key}: registry wiring has no deliverable target")
