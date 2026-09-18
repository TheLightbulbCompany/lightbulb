"""Re-project an AgentCore owner's ALREADY-connected integrations into their Hermes profiles.

Both projection rails run only on a fresh connect. ``hermes_cli.project`` has exactly one caller
(``apply_connection._apply_connection_agentcore``), and ``reconcile_integrations`` is called from
connect / disconnect / the manual-MCP writes / a marketplace deploy — nothing on the MIGRATION
path calls either. ``hermes_provisioning.write_profiles`` writes only the five
``BACKEND_OWNED_FILES``, so a workspace migrated off ECS lands with profiles that carry no
``home/``, no ``mcp-tokens/`` and no ``cli-env.json`` while every ``integrations`` row still reads
"connected": ``gh`` answers "You are not logged into any GitHub hosts", ``config.yaml`` declares
three OAuth MCP servers with no token file to authenticate them with, and nothing tells the owner.

This walks those rows once and re-runs the SAME two projections a connect would, for every
provider whose credential the backend still HAS:

  * MCP-shaped (``mcp_server``/``mcp_remote``/``mcp_stdio``) — covered by ONE
    ``reconcile_integrations(owner_id, agentcore=True)`` at the end, never a per-provider call:
    that function already rebuilds every profile's ``mcp_servers``, ``.env`` and ``mcp-tokens/``
    in one fan-out.
  * a CLI materializer ``hermes_cli.supports`` — ``hermes_cli.project`` with the retained secret.

**What it can NOT fix.** ``refresh_owner: actor`` providers (google, x, apollo, stripe,
instagram, …) retain no secret at all: the container's own keyring/token file was the sole
custodian and it stayed on EFS. There is nothing to re-project, so they are reported
``needs_reconnect`` and the owner must Disconnect → Connect. Run this pass FIRST, because
Disconnect is destructive for google — it revokes every connected account's grant at Google and
deletes every per-agent assignment, which this pass neither needs nor can restore.

``openclaw_paths`` providers (``parallel``) have their own Hermes projection in
``apply_connection.HERMES_OPENCLAW_PATHS_PROVIDERS`` and are NOT re-projected here — they report
``unsupported``, and a reconnect projects them cleanly.
"""

from __future__ import annotations

import logging

from core.observability import put_metric
from core.repositories import integrations_repo
from core.services import runtime_transport
from core.services.credentials import hermes_cli, integration_secret_store, registry
from core.services.credentials.apply_connection import is_mcp_shaped

logger = logging.getLogger(__name__)

PROJECTED = "projected"
NEEDS_RECONNECT = "needs_reconnect"
UNSUPPORTED = "unsupported"


async def reproject_connections(owner_id: str, *, dry_run: bool = False) -> dict[str, str]:
    """``{provider: "projected" | "needs_reconnect" | "unsupported" | "error:<Type>"}``.

    Never raises for one provider: a failure is recorded as ``error:<ExceptionType>`` and the
    walk continues, because the operator needs the outcome for every OTHER provider more than it
    needs this call to stop. Returns ``{}`` for a non-AgentCore owner — an ECS container reads
    its credentials off EFS, where they already are.

    ``dry_run`` takes the same decisions and makes neither write.
    """
    if not await runtime_transport.is_agentcore_owner(owner_id):
        logger.info("reproject: owner=%s is not on agentcore — nothing to project", owner_id)
        return {}

    outcomes: dict[str, str] = {}
    mcp_providers: list[str] = []
    for row in await integrations_repo.list_for_owner(owner_id):
        provider = row.get("provider")
        if not provider or row.get("status", "connected") != "connected":
            continue
        try:
            p = registry.get_provider(provider)
        except KeyError:
            # Not a registry provider: the reserved ``manual_mcp`` / ``projects/<provider>``
            # rows, or an orphan row for a since-removed integration. Same skip as
            # ``build_connected_integration_servers``'.
            continue
        if p.wiring.backend_only:
            continue  # the credential never leaves the backend — there is nothing to project
        # One fetch, two decisions: it is both ``hermes_cli.project``'s argument and the thing
        # ``reconcile_integrations`` seeds ``mcp-tokens/<server>.json`` from, so its absence is
        # exactly what makes a provider unfixable without the owner.
        credential = await integration_secret_store.get(owner_id, provider)
        if is_mcp_shaped(p):
            outcomes[provider] = PROJECTED if credential else NEEDS_RECONNECT
            mcp_providers.append(provider)
            continue
        materializer = p.wiring.materializer
        if not hermes_cli.supports(materializer):
            outcomes[provider] = UNSUPPORTED
        elif not credential:
            outcomes[provider] = NEEDS_RECONNECT
        elif dry_run:
            outcomes[provider] = PROJECTED
        else:
            try:
                await hermes_cli.project(owner_id, provider, materializer, credential)
            except Exception as exc:  # noqa: BLE001 — one provider's failure must not hide the rest
                logger.exception("reproject: owner=%s provider=%s projection failed", owner_id, provider)
                outcomes[provider] = f"error:{type(exc).__name__}"
            else:
                outcomes[provider] = PROJECTED

    if mcp_providers and not dry_run:
        # Lazy, like ``apply_connection``'s own import of it — keeps the module-load dependency
        # between these two one-directional.
        from core.services.credentials.mcp_server import reconcile_integrations

        try:
            await reconcile_integrations(owner_id, agentcore=True)
        except Exception as exc:  # noqa: BLE001 — same rule: report it, per provider, and finish
            logger.exception("reproject: owner=%s MCP reconcile failed", owner_id)
            for provider in mcp_providers:
                outcomes[provider] = f"error:{type(exc).__name__}"

    if not dry_run:
        for provider, outcome in outcomes.items():
            # ``outcome.split(":")[0]`` keeps the dimension bounded — the dict carries the
            # exception type, the metric only carries "error".
            put_metric("hermes.reproject", dimensions={"provider": provider, "outcome": outcome.split(":", 1)[0]})
    return outcomes
