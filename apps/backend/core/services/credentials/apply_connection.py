"""Wiring engine: take a fetched credential for (owner, provider) and deliver it
into the owner's container via the rail the registry specifies."""

from __future__ import annotations

import logging
from typing import Any

from core.dynamodb import run_in_thread
from core.services import config_patcher, runtime_transport
from core.services.credentials import registry
from core.services.credentials.materializers import MATERIALIZERS

logger = logging.getLogger(__name__)


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


def is_mcp_shaped(p: registry.Provider) -> bool:
    """Whether connecting ``p`` produces an entry in
    ``credentials.mcp_server.build_connected_integration_servers`` — the set
    ``hermes_profiles.refresh_integrations`` knows how to project into an
    AgentCore owner's Hermes profiles (M6-R24).

    True for the ``mcp_server`` wiring rail and for the ``mcp_remote``/
    ``mcp_stdio`` provider-level specs — the latter two ride the materializer
    rail for their actual OAuth/file exchange (mcp_oauth, qbo_dotenv) but still
    own an ``mcp.servers.<key>`` entry, so they are MCP-shaped despite not
    using the ``mcp_server`` wiring. Everything else that only carries a
    materializer (gh_cli/xurl_cli/skill_cli/instagram_cli/gog_cli/aws_cli) is a
    CLI-skill provider with no Hermes equivalent: its exec target is the
    OWNER'S MAC via the lightbulb_node terminal backend, never the microVM."""
    return bool(p.mcp_remote or p.mcp_stdio or p.wiring.mcp_server)


async def _apply_connection_agentcore(owner_id: str, provider_key: str, p: registry.Provider) -> None:
    """M6-R24: the AgentCore projection step for a connect/reconnect.

    Never raises and never touches EFS. A pure CLI-skill materializer has no
    Hermes equivalent and is skipped with an INFO log naming the provider and
    the reason; every other rail (mcp_server/mcp_remote/mcp_stdio/
    openclaw_paths) gets every one of the owner's connected integrations
    re-projected into every Hermes profile in ONE shot via the existing
    ``reconcile_integrations`` AgentCore branch — not a per-provider write."""
    if p.wiring.materializer and not is_mcp_shaped(p):
        logger.info(
            "agentcore: skipping %s materializer for owner=%s — exec runs on the owner's Mac, no Hermes equivalent",
            provider_key,
            owner_id,
        )
        return None
    from core.services.credentials.mcp_server import reconcile_integrations

    try:
        await reconcile_integrations(owner_id)
    except Exception:  # noqa: BLE001 — a connect must never fail because the profile refresh did
        logger.exception("agentcore: profile refresh failed for owner=%s provider=%s", owner_id, provider_key)
    return None


async def apply_connection(owner_id: str, provider_key: str, credential: dict[str, Any]) -> Any:
    """credential is the resolved token payload ({"token": ...} for paste, or
    {"access_token","refresh_token",...} for OAuth). Async because config_patcher is async.

    Returns whatever the wiring produced as an identity for the connection — the
    materializer rail surfaces its return (gog_cli returns the connected Google
    account's email). The config-delivery rails have nothing to surface and
    return None, which a generic caller can ignore.

    M6-R24: on an AgentCore owner the projection target is a Hermes profile in
    S3, not openclaw.json on EFS — see ``_apply_connection_agentcore``."""
    p = registry.get_provider(provider_key)
    w = p.wiring
    if w.backend_only:
        # Nothing reaches the container. The credential's only custodian is
        # Secrets Manager, written by the caller via `_retains_secret`.
        return None
    if await runtime_transport.is_agentcore_owner(owner_id):
        return await _apply_connection_agentcore(owner_id, provider_key, p)
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
