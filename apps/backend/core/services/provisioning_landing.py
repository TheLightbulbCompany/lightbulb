"""Where a provisioned credential goes after Friday hands it back.

Friday runs the Stripe CLI in her own container and POSTs the result to
``/company/provisioning/u/{owner}/landed``. This module is the platform's half.
Three writes, in this order and for these reasons:

1. **Secrets Manager** — ``isol8/{env}/integrations/{owner}/{provider}``, the
   EXISTING integration store. Spec §3.5: backend copies of provider
   credentials live here, "never DynamoDB". First, so a crash after this point
   leaves a recoverable credential rather than a provider resource we forgot.
2. **The integrations row** — POINTERS only (resource id, name, service,
   status, env var NAMES, dashboard URL) plus ``actor: "projects"``.
3. **The container** — ``env.vars.<NAME>`` in openclaw.json, OpenClaw's
   documented "environment variable overrides merged into runtime process
   environment". Static env is the right class here: these are provider WRITE
   tokens scoped to the owner's own resources (spec §3.5). The connected-account
   Stripe key and anything else with money authority must ride the credential
   PROXY (``apps/infra/openclaw/plugins/aws-exec-profile``) instead — a named
   follow-up, deliberately NOT in P0b.

Nothing here logs a credential value.
"""

from __future__ import annotations

import logging

from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    provider = resource["provider"]
    # The Secrets Manager blob is per-(owner, provider) and a provider can hold
    # several resources, so MERGE rather than replace — a second `projects add`
    # for the same provider must not blank the first one's variables. A failing
    # read propagates deliberately: writing without knowing what is already
    # there would destroy credentials the provider only ever shows once.
    existing = await integration_secret_store.get(owner_id, provider) or {}
    merged = {**{k: v for k, v in existing.items() if isinstance(v, str)}, **env}
    await integration_secret_store.put(owner_id, provider, merged)
    await integrations_repo.record_provider_resource(owner_id, provider, resource)
    for name, value in sorted(env.items()):
        # One locked RMW per var, not one write of the whole `env.vars` map:
        # set_openclaw_config_path REPLACES the leaf, so setting the map would
        # drop every var landed by another provider.
        await config_patcher.set_openclaw_config_path(owner_id, ["env", "vars", name], value)
    logger.info(
        "provisioning: landed %s/%s for owner=%s (%d vars)",
        provider,
        resource.get("service"),
        owner_id,
        len(env),
    )


async def build_provisioned_env(owner_id: str) -> dict[str, str]:
    """Every provisioned env var, rebuilt from Secrets Manager.

    A reprovision fully overwrites openclaw.json, so without re-injecting these
    the owner's provider tokens vanish on the next image roll — the identical
    failure ``integration_mcp_servers`` exists to prevent
    (core/containers/config.py:2386). Best-effort: never fail a provision.
    """
    env: dict[str, str] = {}
    try:
        for row in await integrations_repo.list_for_owner(owner_id):
            if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
                continue
            if row.get("status", "connected") != "connected":
                continue
            cred = await integration_secret_store.get(owner_id, row.get("provider") or "")
            for name, value in (cred or {}).items():
                if isinstance(value, str):
                    env[name] = value
    except Exception:  # noqa: BLE001 — provisioning must not fail over this.
        logger.exception("build_provisioned_env failed for %s; continuing without it", owner_id)
        return {}
    return env


async def provisioning_summary(owner_id: str) -> tuple[str, list[dict]]:
    """``("paired"|"unpaired", resource pointers)`` from ONE integrations query.

    The state is DERIVED, not probed: a successful `landed` post proves the
    owner completed pairing, and probing the container costs a websocket round
    trip on a path that runs on every billing mutation. Never raises — both
    callers (the Accounts route and the facts file) must survive a DDB hiccup.
    """
    try:
        rows = await integrations_repo.list_for_owner(owner_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("provisioning_summary: integrations read failed for %s: %s", owner_id, e)
        return "unpaired", []
    resources: list[dict] = []
    paired = False
    for row in rows:
        if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
            continue
        paired = True
        resources.extend(row.get("provider_resources") or [])
    resources.sort(key=lambda r: (r.get("provider") or "", r.get("service") or ""))
    return ("paired" if paired else "unpaired"), resources
