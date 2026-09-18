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


class RuntimeUnsupportedError(RuntimeError):
    """Ruling M6-R31: an owner tried to connect an ``openclaw_paths``-wiring provider that has
    no known Hermes/AgentCore equivalent. Raised (unlike the silent CLI-skill materializer skip
    in ``_apply_connection_agentcore``, which the row still records as connected) so the row is
    NEVER marked connected and the caller can answer a specific, actionable error instead of the
    generic ``apply_failed``/502."""


#: openclaw_paths-wiring providers this bridge can actually project onto a Hermes profile
#: (M6-R31). ``parallel`` is the only live example (task-9-scan.md) -- verified directly against
#: the image: Hermes reads its key straight off the environment (``tools/web_tools.py``'s
#: ``_has_env("PARALLEL_API_KEY")``/``_env_value``, ``hermes_cli/tools_config_providers.py``'s own
#: provider->env-var map), never via a config.yaml field of its own, so ``env_key`` is the whole
#: credential delivery. ``source`` names which of the provider's OWN ``openclaw_paths`` op sources
#: (registry.yaml) carries that credential value -- resolved via ``_resolve_source`` like the ECS
#: branch does, rather than assuming ``credential["token"]`` (fix round 1, minor). ``config_patch``
#: additionally selects this backend deterministically (``web.backend`` -- "strict, no probe" per
#: that module's own docstring) instead of leaving Hermes's keyless ring to pick among whichever
#: vendor happens to have a key, matching parity with OpenClaw's explicit
#: ``tools.web.search.provider`` selection; ``clear_patch`` reverts that selector on disconnect. A
#: provider not listed here has no known Hermes equivalent yet -- see RuntimeUnsupportedError above.
HERMES_OPENCLAW_PATHS_PROVIDERS: dict[str, dict[str, Any]] = {
    "parallel": {
        "env_key": "PARALLEL_API_KEY",
        "source": "input.token",
        "config_patch": {"web": {"backend": "parallel"}},
        "clear_patch": {"web": {"backend": ""}},
    },
}


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
    CLI-skill provider: not MCP-shaped, so ``reconcile_integrations`` has no
    fan-out for it. Since #2749 a Hermes turn's shell runs INSIDE the microVM,
    so those five are projected into the profile by
    ``credentials.hermes_cli`` instead of skipped -- see that module."""
    return bool(p.mcp_remote or p.mcp_stdio or p.wiring.mcp_server)


async def _project_hermes_openclaw_paths(owner_id: str, provider_key: str, value: str) -> None:
    """M6-R31: fan a supported openclaw_paths provider's real credential value + its config.yaml
    selector out to every roster profile via ONE combined per-profile write
    (``hermes_profiles.patch_profile_env_and_config`` — .env and config.yaml land together, with
    ``.revision`` bumped only once both have, so a partial projection is never left silently
    behind). Fix round 1, Important 1: deliberately does NOT catch here — unlike the MCP
    reconcile branch below, this rail has no later re-assert pass to self-heal a missed write, so
    ``connect_paste``'s existing ``apply_failed``/502 rollback is the only thing standing between
    a failed projection and a row that reads "connected" with no working key. Let it raise."""
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    spec = HERMES_OPENCLAW_PATHS_PROVIDERS[provider_key]
    for agent in await load_agent_roster(owner_id):
        await run_in_thread(
            hermes_profiles.patch_profile_env_and_config,
            owner_id,
            agent["id"],
            {spec["env_key"]: value},
            spec["config_patch"],
        )


async def clear_hermes_openclaw_paths_projection(owner_id: str, provider_key: str) -> None:
    """Disconnect twin of ``_project_hermes_openclaw_paths`` -- clears the .env value and
    reverts the config.yaml selector (``clear_patch``) on every roster profile. A no-op for a
    provider ``_apply_connection_agentcore`` never projected in the first place (no entry in
    ``HERMES_OPENCLAW_PATHS_PROVIDERS`` -- that connect always raised ``RuntimeUnsupportedError``
    and never wrote anything to clear). Unlike the connect path, this stays best-effort: the row
    has already committed its disconnect by the time this runs and there is no rollback to
    trigger, matching the MCP reconcile disconnect branch's own "a disconnect must never fail
    because the refresh did" convention."""
    spec = HERMES_OPENCLAW_PATHS_PROVIDERS.get(provider_key)
    if spec is None:
        return
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    try:
        for agent in await load_agent_roster(owner_id):
            await run_in_thread(
                hermes_profiles.patch_profile_env_and_config,
                owner_id,
                agent["id"],
                {spec["env_key"]: ""},
                spec["clear_patch"],
            )
    except Exception:  # noqa: BLE001 — a disconnect must never fail because the projection did
        logger.exception(
            "agentcore: hermes openclaw_paths clear failed for owner=%s provider=%s", owner_id, provider_key
        )


async def _apply_connection_agentcore(
    owner_id: str, provider_key: str, p: registry.Provider, credential: dict[str, Any]
) -> None:
    """M6-R24: the AgentCore projection step for a connect/reconnect.

    Never touches EFS. A pure CLI-skill materializer is projected into every Hermes profile by
    ``credentials.hermes_cli`` (which supersedes this branch's original blanket skip -- shell
    commands run in the microVM since #2749, not on the owner's Mac); one whose materializer has
    no projection is still skipped with an INFO log. Every mcp_server/mcp_remote/mcp_stdio
    rail gets every one of the owner's connected integrations re-projected into every Hermes
    profile in ONE shot via the existing ``reconcile_integrations`` AgentCore branch — not a
    per-provider write — and never raises for that path (a self-healing re-assert exists).

    M6-R31: an ``openclaw_paths``-wiring provider (never MCP-shaped) is NOT covered by
    ``reconcile_integrations`` at all (it only projects ``mcp_servers``/``.env`` secrets) — one
    with a known Hermes equivalent (``HERMES_OPENCLAW_PATHS_PROVIDERS``) is projected directly
    and, fix round 1 Important 1, DOES raise on a write failure (no re-assert pass exists for
    this rail, so the caller's rollback must actually see the failure); one without raises
    ``RuntimeUnsupportedError`` so the caller never marks the row connected either way."""
    if p.wiring.materializer and not is_mcp_shaped(p):
        from core.services.credentials import hermes_cli

        if hermes_cli.supports(p.wiring.materializer):
            return await hermes_cli.project(owner_id, provider_key, p.wiring.materializer, credential)
        logger.info(
            "agentcore: skipping %s materializer for owner=%s — no Hermes projection for this rail",
            provider_key,
            owner_id,
        )
        return None
    if p.wiring.openclaw_paths and not is_mcp_shaped(p):
        spec = HERMES_OPENCLAW_PATHS_PROVIDERS.get(provider_key)
        if spec is None:
            raise RuntimeUnsupportedError(f"{provider_key}: no Hermes/AgentCore equivalent for this provider yet")
        value = _resolve_source(spec["source"], credential, credential)
        await _project_hermes_openclaw_paths(owner_id, provider_key, value)
        return None
    from core.services.credentials.mcp_server import reconcile_integrations

    try:
        # agentcore=True: the caller (apply_connection) already resolved this
        # for this same request -- no second DynamoDB read (M6-R26 fix round
        # 1, Important 4). pending=provider_key: this provider's `connected`
        # status row is written by the caller AFTER apply_connection returns
        # (Critical 1) -- without it, the FIRST connect's profile rewrite
        # would never see its own new provider.
        await reconcile_integrations(owner_id, agentcore=True, pending=provider_key)
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
        return await _apply_connection_agentcore(owner_id, provider_key, p, credential)
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
