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

The ``(owner, provider)`` Secrets Manager blob is SHARED with the OAuth
integrations rail — ``_secret_name`` is the same name ``routers/connections.py``
writes and ``credentials/mcp_server.py`` reads, and ``stripe``/``posthog`` are
both registry providers (``credentials/providers.yaml``) AND providers this rail
lands. So nothing here may assume the blob is its own: reads take only the names
the row declares, and writes merge rather than replace.

P0b ships no reconcile entry point for this rail (the integrations rail has
``mcp_server.reconcile_integrations``); a config write missed because the
container has no ``openclaw.json`` yet is repaired by the next provision, which
calls ``build_provisioned_env``.

Nothing here logs a credential value.
"""

from __future__ import annotations

import logging

from core.repositories import integrations_repo
from core.services import config_patcher
from core.services.config_patcher import ConfigPatchError
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    provider = resource["provider"]
    # The Secrets Manager blob is per-(owner, provider) and a provider can hold
    # several resources, so MERGE rather than replace — a second `projects add`
    # for the same provider must not blank the first one's variables. A failing
    # read propagates deliberately: writing without knowing what is already
    # there would destroy credentials the provider only ever shows once.
    #
    # Merged VERBATIM, with no type filter: the same blob holds the OAuth rail's
    # credential for this provider, whose fields are not all strings
    # (``expires_in: int | None``). Dropping the non-strings would corrupt the
    # round-trip the store documents.
    existing = await integration_secret_store.get(owner_id, provider) or {}
    merged = {**existing, **env}
    await integration_secret_store.put(owner_id, provider, merged)
    await integrations_repo.record_provider_resource(owner_id, provider, resource)
    try:
        for name, value in sorted(env.items()):
            # One locked RMW per var, not one write of the whole `env.vars` map:
            # set_openclaw_config_path REPLACES the leaf, so setting the map would
            # drop every var landed by another provider.
            await config_patcher.set_openclaw_config_path(owner_id, ["env", "vars", name], value)
    except ConfigPatchError:
        # No openclaw.json yet (container not provisioned). Not an error: the
        # credential is already durable, and the provision that creates the config
        # injects it via build_provisioned_env. Same call as
        # mcp_server.reconcile_integrations makes.
        logger.info(
            "provisioning: no container config yet for owner=%s; %s vars land at provision",
            owner_id,
            provider,
        )
    logger.info(
        "provisioning: landed %s/%s for owner=%s (%d vars)",
        provider,
        resource.get("service"),
        owner_id,
        len(env),
    )


async def revoke_credentials(*, owner_id: str, provider: str, resource_id: str) -> bool:
    """Delete OUR copy of one provisioned resource: the credential and pointer.

    The unwind side of ``land_credentials``. Returns ``False`` — and touches
    NOTHING (no secret read/write, no config change, no pointer removal) —
    when no pointer with this ``resource_id`` exists for the provider: a
    retry or a stale resource_id must be a no-op, never a partial revoke.

    Deliberately does NOT remove the provider resource itself: it is the
    owner's and stays theirs (spec §3.6). Removing it is a separate, explicit
    action they take in the provider's own dashboard.

    The ``(owner, provider)`` Secrets Manager blob is SHARED with the OAuth
    integrations rail (see the module docstring above), so this must never
    delete the whole blob — that would also destroy an OAuth credential the
    owner connected separately. Instead it drops only the REVOKED resource's
    own ``env_keys`` from the blob, and force-deletes the secret only when
    nothing is left. ``get`` returning ``None`` (already gone) skips the
    secret step entirely rather than writing back an empty dict.

    Order: trim the shared blob, then strip the same names from the
    container's ``env.vars``, then remove the pointer LAST — so a retry
    after a config-patch failure can still find the pointer's ``env_keys``
    to finish the job.
    """
    resources = await integrations_repo.list_provider_resources(owner_id, provider)
    target = next((r for r in resources if r.get("resource_id") == resource_id), None)
    if target is None:
        return False
    env_keys = list(target.get("env_keys") or [])

    if env_keys:
        blob = await integration_secret_store.get(owner_id, provider)
        if blob is not None:
            for key in env_keys:
                blob.pop(key, None)
            if blob:
                await integration_secret_store.put(owner_id, provider, blob)
            else:
                await integration_secret_store.delete(owner_id, provider)
        try:
            for key in env_keys:
                # One locked RMW per var, same reasoning as land_credentials:
                # a single "delete the whole map" write would drop every var
                # landed by another provider.
                await config_patcher.delete_openclaw_config_path(owner_id, ["env", "vars", key])
        except ConfigPatchError:
            # No openclaw.json yet (container not provisioned) or the path is
            # already gone. Not an error: the Secrets Manager copy is already
            # gone, which is what matters, and the pointer removal below is
            # still safe to retry.
            logger.info(
                "provisioning: no container config yet for owner=%s; %s revoke skips config strip",
                owner_id,
                provider,
            )

    await integrations_repo.remove_provider_resource(owner_id, provider, resource_id)
    logger.info("provisioning: revoked %s/%s for owner=%s", provider, resource_id, owner_id)
    return True


async def build_provisioned_env(owner_id: str) -> dict[str, str]:
    """Every provisioned env var, rebuilt from Secrets Manager.

    A reprovision fully overwrites openclaw.json, so without re-injecting these
    the owner's provider tokens vanish on the next image roll — the identical
    failure the ``integration_mcp_servers`` merge in ``build_openclaw_config_dict``
    exists to prevent. Best-effort: never fail a provision.

    Only the names the row DECLARES (the union of its
    ``provider_resources[].env_keys``) are read out of the blob, never the blob
    itself: it is shared with the OAuth rail, so a provider connected on both
    would otherwise leak ``access_token``/``refresh_token``/``client_secret``
    into openclaw.json.
    """
    env: dict[str, str] = {}
    try:
        for row in await integrations_repo.list_for_owner(owner_id):
            if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
                continue
            if row.get("status", "connected") != "connected":
                continue
            names = {
                name for resource in row.get("provider_resources") or [] for name in resource.get("env_keys") or []
            }
            if not names:
                continue
            cred = await integration_secret_store.get(owner_id, row.get("provider") or "") or {}
            for name in names:
                value = cred.get(name)
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
