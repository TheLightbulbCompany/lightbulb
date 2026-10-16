"""Where a provisioned credential goes once ``provisioning_workflow._bind`` has
read it out of ``environment_info``. Three writes, in this order and for these
reasons:

1. **Secrets Manager** — ``lightbulb/{env}/integrations/{owner}/projects/{provider}``,
   the EXISTING integration store, under this rail's OWN namespace. Spec §3.5:
   backend copies of provider credentials live here, "never DynamoDB". First,
   so a crash after this point leaves a recoverable credential rather than a
   provider resource we forgot.
2. **The integrations row** — POINTERS only (resource id, name, service,
   status, env var NAMES, dashboard URL) plus ``actor: "projects"``, keyed
   ``projects/{provider}``.
3. **Every roster profile's ``.env``** on the owner's AgentCore runtime, through
   ``hermes_profiles.patch_profile_env_and_config`` — the same path a connector's secret
   takes (spec 2026-10-15 §2.10). ``build_provisioned_env`` re-reads the Secrets Manager
   copy at every provision, so a profile created later gets the same values. These are
   provider WRITE tokens scoped to the owner's own resources (spec §3.5); names the runtime
   itself owns (``RESERVED_ENV_PREFIXES`` / ``RESERVED_ENV_NAMES``) are refused before any
   write, so a provider can never shadow the agent's own credentials. The connected-account
   Stripe key and anything else with money authority was meant to ride a credential proxy
   instead; that is still a named follow-up, so the distinction is currently unenforced.

Revoke is the unwind: it trims the revoked resource's keys from the blob, blanks the same
names in every roster profile's ``.env`` (an empty value deletes the key), then removes the
pointer.

**Both storage keys are namespaced** via ``integrations_repo.projects_key`` —
the ``integrations`` sort key AND the Secrets Manager provider segment. The
BARE ``(owner, provider)`` key belongs to the OAuth integrations rail, and
``stripe``/``posthog``/``sentry``/``cloudflare`` are registry providers
(``credentials/providers.yaml``) as well as providers this rail lands. Sharing
it made six OAuth readers treat a provisioned provider as OAuth-connected, 409
the OAuth start, and — worst — let an OAuth Disconnect force-delete this rail's
credentials. The namespaced key is invisible to those readers (it matches no
registry provider), so nothing else has to change to stay correct.

The blob is therefore this rail's own. The merge-rather-than-replace writes and
the read-only-declared-names reads below STAY: one provider can hold several
provisioned resources, so a second ``projects add`` must not blank the first's
variables, and injecting the whole blob would put any future non-env field into
the agents' environment.

Nothing here logs a credential value.
"""

from __future__ import annotations

import asyncio
import logging

from core.repositories import integrations_repo
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

# Names the runtime owns: a provider credential must never shadow them in a profile's ``.env``.
RESERVED_ENV_PREFIXES = ("PAPERCLIP_", "MCP_", "GOG_", "LIGHTBULB_", "ISOL8_", "OPENCLAW_", "HERMES_", "AWS_")
RESERVED_ENV_NAMES = frozenset({"ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"})


async def _roster(owner_id: str) -> list[str]:
    from core.services.credentials.hermes_cli import _roster as roster

    return await roster(owner_id)


def _patch_profile_env(owner_id: str, agent_id: str, env: dict[str, str]) -> None:
    from core.services.hermes_profiles import patch_profile_env_and_config

    patch_profile_env_and_config(owner_id, agent_id, env, {})


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    """Three writes, in the module docstring's order: the provider's blob in Secrets Manager
    (merged, so a second resource of the same provider keeps the first's keys), the pointer row,
    then the values into every roster profile's ``.env`` (spec 2026-10-15 §2.10), the same path a
    connector's secret takes. A profile write that fails raises AFTER the first two landed, so a
    retry finds the credential and the pointer and only has to deliver again."""
    for name in env:
        if name.startswith(RESERVED_ENV_PREFIXES) or name in RESERVED_ENV_NAMES:
            from core.services.provisioning_workflow import WorkflowRefused  # circular at module level

            raise WorkflowRefused("reserved_env_name", f"Provider credential name {name!r} is reserved on this runtime")
    provider = resource["provider"]
    if env:
        secret_key = integrations_repo.projects_key(provider)
        blob = dict(await integration_secret_store.get(owner_id, secret_key) or {})
        blob.update(env)
        await integration_secret_store.put(owner_id, secret_key, blob)
    await integrations_repo.record_provider_resource(owner_id, provider, resource)
    if not env:
        return
    for agent_id in await _roster(owner_id):
        await asyncio.to_thread(_patch_profile_env, owner_id, agent_id, env)
    logger.info(
        "provisioning: landed %s/%s for owner=%s keys=%s", provider, resource.get("resource_id"), owner_id, sorted(env)
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

    One provider's blob holds every resource that provider landed, so this
    drops only the REVOKED resource's own ``env_keys`` — minus any name a
    SURVIVING resource of the same provider still declares, which would
    otherwise be stripped out from under it — and force-deletes the secret
    only when nothing is left. ``get`` returning ``None`` (already gone)
    skips the secret step entirely rather than writing back an empty dict.

    Order: trim the blob, then strip the same names from every roster
    profile's ``.env``, then remove the pointer LAST — so a retry after a
    config-patch failure can still find the pointer's ``env_keys`` to finish
    the job.
    """
    resources = await integrations_repo.list_provider_resources(owner_id, provider)
    target = next((r for r in resources if r.get("resource_id") == resource_id), None)
    if target is None:
        return False
    still_used = {name for r in resources if r.get("resource_id") != resource_id for name in r.get("env_keys") or []}
    env_keys = [k for k in (target.get("env_keys") or []) if k not in still_used]

    if env_keys:
        secret_key = integrations_repo.projects_key(provider)
        blob = await integration_secret_store.get(owner_id, secret_key)
        if blob is not None:
            for key in env_keys:
                blob.pop(key, None)
            if blob:
                await integration_secret_store.put(owner_id, secret_key, blob)
            else:
                await integration_secret_store.delete(owner_id, secret_key)
        # Clear the same names from every roster profile's ``.env`` (an empty value deletes the
        # key), so the agent stops holding the revoked secret.
        for agent_id in await _roster(owner_id):
            await asyncio.to_thread(_patch_profile_env, owner_id, agent_id, {k: "" for k in env_keys})

    await integrations_repo.remove_provider_resource(owner_id, provider, resource_id)
    logger.info("provisioning: revoked %s/%s for owner=%s", provider, resource_id, owner_id)
    return True


async def build_provisioned_env(owner_id: str) -> dict[str, str]:
    """Every provisioned env var, rebuilt from Secrets Manager.

    A reprovision fully overwrites openclaw.json, so without re-injecting these
    the owner's provider tokens vanish on the next image roll — the identical
    failure the ``integration_mcp_servers`` merge in ``build_openclaw_config_dict``
    exists to prevent. Best-effort: never fail a provision.

    Rows are selected by BOTH the ``projects/`` key prefix and the ``actor``
    stamp, so an OAuth row can never be read through here even if a future
    writer stamps the actor by mistake.

    Only the names the row DECLARES (the union of its
    ``provider_resources[].env_keys``) are read out of the blob, never the blob
    itself — openclaw.json gets exactly the variables this rail landed.
    """
    env: dict[str, str] = {}
    try:
        for row in await integrations_repo.list_for_owner(owner_id):
            key = row.get("provider") or ""
            if not key.startswith(integrations_repo.PROJECTS_KEY_PREFIX):
                continue
            if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
                continue
            names = {
                name for resource in row.get("provider_resources") or [] for name in resource.get("env_keys") or []
            }
            if not names:
                continue
            cred = await integration_secret_store.get(owner_id, key) or {}
            for name in names:
                value = cred.get(name)
                if isinstance(value, str):
                    env[name] = value
    except Exception:  # noqa: BLE001 — provisioning must not fail over this.
        logger.exception("build_provisioned_env failed for %s; continuing without it", owner_id)
        return {}
    return env


async def provisioning_summary(owner_id: str) -> list[dict]:
    """Every provisioned-resource pointer this owner has, from ONE integrations
    query. Never raises — the one caller (``GET /resources``) must survive a DDB
    hiccup, and an empty list is the honest answer either way."""
    try:
        rows = await integrations_repo.list_for_owner(owner_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("provisioning_summary: integrations read failed for %s: %s", owner_id, e)
        return []
    resources: list[dict] = []
    for row in rows:
        # Same two-part selection as build_provisioned_env. The pointers inside
        # carry the BARE provider (`posthog`), never the namespaced key — that
        # is what /resources renders and what revoke takes back.
        if not (row.get("provider") or "").startswith(integrations_repo.PROJECTS_KEY_PREFIX):
            continue
        if row.get("actor") != integrations_repo.PROJECTS_ACTOR:
            continue
        resources.extend(row.get("provider_resources") or [])
    resources.sort(key=lambda r: (r.get("provider") or "", r.get("service") or ""))
    return resources
