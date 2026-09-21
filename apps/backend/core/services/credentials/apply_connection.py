"""Wiring engine: take a fetched credential for (owner, provider) and deliver it
into the owner's container via the rail the registry specifies."""

from __future__ import annotations

import logging
from typing import Any

from core.dynamodb import run_in_thread
from core.services.credentials import registry

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
    """The provider has no usable credential path in the current Hermes runtime."""


#: openclaw_paths-wiring providers this bridge can actually project onto a Hermes profile
#: (M6-R31). ``parallel`` is the only live example (task-9-scan.md) -- verified directly against
#: the image: Hermes reads its key straight off the environment (``tools/web_tools.py``'s
#: ``_has_env("PARALLEL_API_KEY")``/``_env_value``, ``hermes_cli/tools_config_providers.py``'s own
#: provider->env-var map), never via a config.yaml field of its own, so ``env_key`` is the whole
#: credential delivery. ``source`` names which of the provider's OWN ``openclaw_paths`` op sources
#: (registry.yaml) carries that credential value -- resolved via ``_resolve_source`` like the openclaw.json
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


def runtime_supported(p: registry.Provider) -> bool:
    """Whether this provider has a credential path the current Hermes runtime can use."""
    if p.wiring.backend_only:
        return True
    if is_mcp_shaped(p):
        # Reconciliation reads Secrets Manager for brokered grants; an unbrokered
        # actor-owned grant has no durable backend copy and cannot be projected.
        if p.mcp_stdio and p.wiring.materializer:
            from core.services.credentials import hermes_cli

            return hermes_cli.supports(p.wiring.materializer)
        return registry.refresh_owner_for(p) != "actor" or p.retain_secret
    if p.wiring.materializer:
        from core.services.credentials import hermes_cli

        return hermes_cli.supports(p.wiring.materializer)
    return not p.wiring.openclaw_paths or p.key in HERMES_OPENCLAW_PATHS_PROVIDERS


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
    """Project a supported connection; let failures reach the caller's rollback."""
    if not runtime_supported(p):
        raise RuntimeUnsupportedError(f"{provider_key}: no usable Hermes credential path")
    if p.wiring.materializer and (not is_mcp_shaped(p) or p.mcp_stdio):
        from core.services.credentials import hermes_cli

        result = await hermes_cli.project(owner_id, provider_key, p.wiring.materializer, credential)
        if p.mcp_stdio:
            from core.services.credentials.mcp_server import reconcile_integrations

            await reconcile_integrations(owner_id, pending=provider_key)
        return result
    if p.wiring.openclaw_paths and not is_mcp_shaped(p):
        spec = HERMES_OPENCLAW_PATHS_PROVIDERS.get(provider_key)
        if spec is None:
            raise RuntimeUnsupportedError(f"{provider_key}: no Hermes/AgentCore equivalent for this provider yet")
        value = _resolve_source(spec["source"], credential, credential)
        await _project_hermes_openclaw_paths(owner_id, provider_key, value)
        return None
    from core.services.credentials.mcp_server import reconcile_integrations

    # The row is written after apply_connection returns; the pending key makes
    # the first projection include this provider before that status write.
    await reconcile_integrations(owner_id, pending=provider_key)
    return None


async def apply_connection(owner_id: str, provider_key: str, credential: dict[str, Any]) -> Any:
    """credential is the resolved token payload ({"token": ...} for paste, or
    {"access_token","refresh_token",...} for OAuth).

    Returns whatever the wiring produced as an identity for the connection — the
    materializer rail surfaces its return (gog_cli returns the connected Google
    account's email).

    M6-R24: the projection target is a Hermes profile in S3 — see
    ``_apply_connection_agentcore``, which is now the only delivery path. The
    openclaw.json rails (``wiring.openclaw_paths``, the direct
    ``mcp.servers.<key>`` write) went with the file."""
    p = registry.get_provider(provider_key)
    if p.wiring.backend_only:
        # Nothing reaches the runtime. The credential's only custodian is
        # Secrets Manager, written by the caller via `_retains_secret`.
        return None
    return await _apply_connection_agentcore(owner_id, provider_key, p, credential)
