"""Wiring engine: take a fetched credential for (owner, provider) and deliver it
into the owner's container via the rail the registry specifies."""

from __future__ import annotations

from typing import Any

from core.services import config_patcher
from core.services.credentials import registry
from core.services.credentials.materializers import MATERIALIZERS


def _resolve_source(source: str, credential: dict[str, Any], raw_input: dict[str, Any]) -> Any:
    # "input.token" -> raw_input["token"]; "credentials.access_token" -> credential["access_token"]; dotted = nested.
    root, *rest = source.split(".")
    cur: Any = raw_input if root == "input" else credential
    for part in rest:
        cur = cur[part]
    return cur


async def apply_connection(owner_id: str, provider_key: str, credential: dict[str, Any]) -> None:
    """credential is the resolved token payload ({"token": ...} for paste, or
    {"access_token","refresh_token",...} for OAuth). Async because config_patcher is async."""
    p = registry.get_provider(provider_key)
    w = p.wiring
    if w.openclaw_paths:
        for op in w.openclaw_paths:
            value = _resolve_source(op.source, credential, credential)
            await config_patcher.set_openclaw_config_path(owner_id, op.path.split("."), value)
        return
    if w.materializer:
        MATERIALIZERS[w.materializer](owner_id, provider_key, credential)  # materializers are sync file writes
        return
    raise RuntimeError(f"{provider_key}: registry wiring has no deliverable target")
