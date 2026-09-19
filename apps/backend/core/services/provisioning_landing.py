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
3. **The agent's runtime env** — ``build_provisioned_env`` re-reads the Secrets Manager
   copy at every provision and lands it in each roster profile's ``.env``
   (``hermes_profiles.write_agent_profile``), so a credential provisioned between two
   provisions reaches the agent on the next one. It used to be ``env.vars.<NAME>`` in
   ``openclaw.json``; the class is the same, which is what matters here: these are provider
   WRITE tokens scoped to the owner's own resources (spec §3.5). The connected-account Stripe
   key and anything else with money authority was meant to ride a credential PROXY instead —
   that proxy was a baked OpenClaw plugin and is gone with it, so the distinction is currently
   unenforced (still a named follow-up, still deliberately not in P0b).

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
openclaw.json.

P0b ships no reconcile entry point for this rail (the integrations rail has
``mcp_server.reconcile_integrations``); a config write missed because the
container has no ``openclaw.json`` yet is repaired by the next provision, which
calls ``build_provisioned_env``.

Nothing here logs a credential value.
"""

from __future__ import annotations

import logging

from core.repositories import integrations_repo
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)


async def land_credentials(*, owner_id: str, resource: dict, env: dict[str, str]) -> None:
    """Refused: there is no delivery mechanism for a landed credential on this runtime.

    It used to write the values into ``openclaw.json["env"]["vars"]`` under the config lock,
    and the profile ``.env`` / secret-scope equivalent is deliberately out of scope. Refusing
    the WHOLE landing -- rather than persisting the secret and the pointer and then failing to
    deliver -- is what keeps the workflow from going "active" over a credential nothing can
    reach. The Stripe Projects rail is off by default anyway
    (``STRIPE_PROVISIONING_ENABLED``).
    """
    from core.services.provisioning_workflow import WorkflowRefused

    raise WorkflowRefused("unsupported_on_agentcore", "Provisioning services for this workspace isn't available yet")


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

    Order: trim the blob, then strip the same names from the container's
    ``env.vars``, then remove the pointer LAST — so a retry after a
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
        # No config to scrub: the plaintext copy used to live in the owner's openclaw.json
        # ``env.vars`` on EFS, and nothing writes an equivalent on this runtime (see
        # ``land_credentials``, which now refuses). The Secrets Manager copy above is the
        # only one there is, and it is gone.

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
