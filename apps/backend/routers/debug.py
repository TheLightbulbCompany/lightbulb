"""
Dev-only container provisioning endpoints.

Bypasses Stripe for local testing — served only when ENVIRONMENT is
dev/local/test or unset (see ``assert_debug_enabled``), refused everywhere else.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, get_owner_type, require_org_admin, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.services.hermes_provisioning import delete_workspace_runtime
from core.repositories import (
    billing_repo,
    channel_link_repo,
    container_repo,
    integrations_repo,
    update_repo,
    usage_repo,
    user_repo,
)
from core.repositories.workspace_repo import WorkspaceRepo
from core.services import connection_service, workspace_memory
from core.services.provision_dispatch import dispatch_provision
from core.services.credentials import integration_secret_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ALLOWLIST, not a blacklist. These endpoints bypass Stripe, wipe a tenant's
# data and probe EFS, so they may exist only in a developer sandbox — any
# ENVIRONMENT value we don't recognise as one is refused. The old
# `== "prod"` test failed OPEN for every value nobody remembered to add:
# `staging` is a legal deployed value (core/config.py _DEPLOYED_ENVIRONMENTS)
# and would have exposed the whole /debug surface the day a staging stage
# was created. Empty = unset = local dev / CI.
_DEBUG_ENVIRONMENTS = frozenset({"dev", "local", "test", ""})


def assert_debug_enabled(endpoint: str = "debug") -> None:
    """Raise 403 unless ENVIRONMENT is a developer sandbox.

    The single gate for every /debug route. Routes that can use a FastAPI
    dependency go through ``require_non_production``; handlers that need the
    per-endpoint metric label call this directly. Do not re-inline the check.
    """
    if settings.ENVIRONMENT not in _DEBUG_ENVIRONMENTS:
        put_metric("debug.endpoint.prod_hit", dimensions={"endpoint": endpoint})
        raise HTTPException(status_code=403, detail="Not available in this environment")


async def require_non_production() -> None:
    """Route-dependency form of :func:`assert_debug_enabled`."""
    assert_debug_enabled()


@router.post(
    "/provision",
    summary="Provision the caller's workspace runtime (dev only)",
    description=(
        "Runs the same provisioning funnel production uses (SFN watchdog + "
        "scripts/provision_runner.py). Only available in non-production environments."
    ),
    operation_id="debug_provision_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test (or unset)"},
    },
)
async def provision_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    owner_type = get_owner_type(auth)

    existing = await container_repo.get_by_owner_id(owner_id)
    if existing and existing.get("status") in ("running", "provisioning"):
        return {"status": "already_running", "owner_id": owner_id, "owner_type": owner_type}

    # The SAME entry point production uses, rather than a hand-rolled sequence: the
    # runner owns the role / gate-token mint, the roster profiles and the AgentCore
    # runtime, in that order. DELETE below destroys the role along with the row, so
    # re-minting here is the only honest recovery -- which is exactly what the runner does. ``dispatch_provision`` never raises; a failed
    # dispatch leaves a retryable error row.
    await dispatch_provision(owner_id)
    return {"status": "provisioning", "owner_id": owner_id, "owner_type": owner_type}


@router.patch(
    "/provision",
    summary="Rewrite the roster profiles and cold-restart the runtime (dev only)",
    description=(
        "Rewrites every roster profile, then stops "
        "the AgentCore session so the next invoke cold-restores instead of a warm microVM "
        "serving whatever it cached mid-turn."
    ),
    operation_id="debug_redeploy_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test (or unset)"},
        404: {"description": "No container found"},
    },
)
async def redeploy_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)
    owner_type = get_owner_type(auth)

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        raise HTTPException(status_code=404, detail="No container found")

    # Row #18: rewrite every roster profile (the same inputs write_profiles sources at fresh
    # provision -- resolve_profile_inputs), then stop the runtime session so the NEXT invoke
    # cold-restores.
    from core.services import hermes_provisioning

    await hermes_provisioning.write_profiles(owner_id)
    runtime_arn = container.get("agentcore_runtime_arn")
    if runtime_arn:
        await hermes_provisioning.stop_runtime_session(owner_id, runtime_arn)  # never raises
    return {
        "status": "profiles_rewritten",
        "owner_id": owner_id,
        "owner_type": owner_type,
    }


@router.delete(
    "/provision",
    summary="Remove the caller's runtime (dev only)",
    description="Tears down the caller's AgentCore runtime and per-owner resources. Dev only.",
    operation_id="debug_remove_container",
    dependencies=[Depends(require_non_production)],
    responses={
        403: {"description": "Not available outside dev/local/test (or unset)"},
        404: {"description": "No container found"},
    },
)
async def remove_container(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    if auth.is_org_context:
        require_org_admin(auth)

    try:
        # NOT delete_profiles: this is a routine reprovision door, and destroying a migrated
        # owner's memory/skills/SOUL.md is /debug/user-data's job, not this one (M6-R42).
        await delete_workspace_runtime(owner_id)
        return {"status": "removed"}
    except Exception as e:  # noqa: BLE001 — surfaced to the dev caller as a 503
        logger.error("Dev remove failed for owner %s: %s", owner_id, e)
        raise HTTPException(status_code=503, detail=str(e))


@router.delete(
    "/user-data",
    summary="DEV/TEST ONLY: atomically delete all per-user data",
    description=(
        "Tears down the owner's AgentCore runtime and every S3 Hermes profile under "
        "their prefix, then rows from all 9 per-user DDB tables (including "
        "paperclip-companies, whose row carries the onboarding "
        "generation/funnel/bootstrap fields — see INV-10). Leaves an empty slate: the "
        "e2e harness calls it at the end of each run and the next provision must not "
        "restore anything. Caller can only delete their own data (owner_id derived from "
        "JWT)."
    ),
    operation_id="debug_delete_user_data",
    responses={
        403: {"description": "Not available outside dev/local/test (or unset)"},
    },
)
async def delete_user_data(auth: AuthContext = Depends(get_current_user)):
    assert_debug_enabled("user-data")

    # Mirror /debug/provision DELETE — non-admin org members must not be able
    # to wipe shared org-scoped state (containers, billing, api-keys, etc.)
    # via this endpoint just because dev/staging exposes it. Personal context
    # passes through (require_org_admin no-ops when not in org).
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)
    # In an org context resolve_owner_id returns the org_id, but the `users`
    # and `ws-connections` tables are keyed by the Clerk user_id directly.
    # Without this split, org E2E teardown would target the org id for both,
    # leaving the Clerk user row + WS connection rows behind on every run.
    user_id = auth.user_id
    deleted: dict = {"runtime": False, "ddb": []}
    # Track failures so we can return 500 at the end. Test cleanup needs to
    # know if anything leaked — a green 200 with a leaked runtime was silently
    # accumulating orphans on every run.
    # Each step is still best-effort (we attempt every step regardless of
    # earlier failures so the spec sees a complete summary), but we surface
    # a non-success status at the end if any destructive step errored.
    failures: list[str] = []

    # ---- Runtime teardown ---------------------------------------------------
    # NOT gated on the row's existence — if a previous partial run failed here but
    # successfully wiped the DDB row, every subsequent retry would skip teardown and
    # orphan the runtime forever. ``delete_workspace_runtime`` derives everything it needs
    # from owner_id and treats "already gone" as idempotent success.
    #
    # ``delete_profiles=True`` is this endpoint's answer to the EFS ``rm -rf`` it replaces:
    # the owner's S3 profile prefix IS their workspace content, and the e2e harness's
    # empty-slate contract breaks silently if the next provision restores it. This and
    # ``account_wipe`` are the ONLY two terminal callers that pass True (M6-R42).
    runtime_succeeded = False
    try:
        report: dict = {}
        await delete_workspace_runtime(owner_id, delete_profiles=True, report=report)
        # "scheduled": a parked member keeps the runtime until it forgets its entries (<= 3 min).
        deleted["runtime"] = "scheduled" if report.get("runtime") == "scheduled" else True
        runtime_succeeded = True
    except Exception as e:
        logger.warning("runtime teardown failed for %s: %s", owner_id, e)
        failures.append(f"runtime: {e}")

    # ---- DynamoDB cleanup, all 8 per-user tables ----------------------------
    # Per-step try/except so one transient throttle on (e.g.) container_repo
    # doesn't abort the rest of the teardown — we want every table attempted,
    # then surface a 500 with the failure list at the end if anything broke.
    # NB: do NOT add ("containers", ...) here unconditionally. If the runtime
    # teardown failed, the row still holds the runtime ARN and gate-token hash
    # the next retry needs — wiping the row turns a transient error into an
    # unrecoverable leak. On the happy path delete_user_service has already
    # removed the row, so the explicit delete here is just belt-and-suspenders.
    async def _wipe_integrations() -> None:
        # Sweep both the secret-free status rows AND the per-provider Secrets
        # Manager credentials. List the rows first so the SM secrets can be
        # deleted by name without an account-wide secretsmanager:ListSecrets.
        for row in await integrations_repo.list_for_owner(owner_id):
            provider = row.get("provider")
            if not provider:
                continue
            # `#`-namespaced rows (e.g. google#account#<email>) are actor-refresh
            # sub-records with no Secrets Manager secret, and the `#` would make
            # the SM delete raise InvalidParameterException. Drop the DDB row
            # only; skip the SM call for them.
            if "#" not in provider:
                await integration_secret_store.delete(owner_id, provider)
            await integrations_repo.delete(owner_id, provider)

    async def _wipe_byo_llm_keys() -> None:
        # BYO LLM keys live in Secrets Manager (user-keys/<owner>/<provider>),
        # keyed by the owner id key_service.set_key used. Force-delete both
        # providers' secrets (no-op if absent). Paperclip per-agent keys also
        # live in Secrets Manager but are not enumerable here without the agent
        # ids; they are re-mintable and swept by the clean-slate Paperclip reset.
        from core.services.key_service import KeyService

        svc = KeyService()
        for provider in ("openai", "anthropic"):
            await svc.delete_key(owner_id, provider)

    ddb_steps = []
    if runtime_succeeded:
        ddb_steps.append(("containers", lambda: container_repo.delete(owner_id)))
    ddb_steps.extend(
        [
            ("billing-accounts", lambda: billing_repo.delete(owner_id)),
            ("byo-llm-keys", _wipe_byo_llm_keys),
            ("usage-counters", lambda: usage_repo.delete_all_for_owner(owner_id)),
            ("pending-updates", lambda: update_repo.delete_all_for_owner(owner_id)),
            ("channel-links", lambda: channel_link_repo.delete_all_for_owner(owner_id)),
            ("integrations", _wipe_integrations),
            ("ws-connections", lambda: connection_service.delete_all_for_user(user_id)),
            ("users", lambda: user_repo.delete(user_id)),
            # The person's own memory data: sessions, drafts, episodes, computer keys.
            ("person-memory", lambda: workspace_memory.delete_person_memory(user_id)),
            # The WORKSPACE row (keyed by the owner id) carries the company,
            # its onboarding generation/funnel and the starter roster (INV-10).
            # Left behind, the next fresh provision would RECONCILE-ADOPT the
            # old company instead of minting a new onboarding_generation. The
            # caller's identity row is kept: it is their Better Auth account's
            # only password, and this endpoint deletes no Better Auth user.
            (
                "paperclip-companies",
                lambda: WorkspaceRepo().delete(owner_id),
            ),
            # The pending-brief sentinel lives in the SAME table under a
            # synthetic PK, so the row delete above does not reach it.
            # Left behind it outlives the teardown by design -- a pending
            # brief deliberately survives reprovisioning -- and since this
            # endpoint does not delete the Clerk account, the next signup
            # reuses the same owner_id and inherits the previous run's answer:
            # the funnel never renders, and the new company gets seeded from
            # the old brief. That is the zero-orphan (INV-10) break the
            # comment above exists to prevent.
            (
                "paperclip-companies:pending-brief",
                lambda: WorkspaceRepo().delete_pending_brief(owner_id),
            ),
        ]
    )
    for table_name, action in ddb_steps:
        try:
            await action()
            deleted["ddb"].append(table_name)
        except Exception as e:
            logger.warning("DDB teardown failed for %s on %s: %s", table_name, owner_id, e)
            failures.append(f"ddb.{table_name}: {e}")

    if failures:
        # 500 with the failure list AND the partial deleted summary so the
        # caller can both fail loudly AND know what state was reached.
        raise HTTPException(
            status_code=500,
            detail={"deleted": deleted, "failures": failures},
        )

    return {"deleted": deleted}


@router.get(
    "/ddb-rows",
    summary="DEV/TEST ONLY: row counts per per-user DDB table for an owner",
    description=(
        "Returns a row count for each of the 8 per-user DynamoDB tables, "
        "scoped to the given ``owner_id``. Used by the e2e teardown "
        "verification pass to assert the DELETE ``/debug/user-data`` call "
        "actually drained every table."
    ),
    operation_id="debug_ddb_rows",
    responses={
        403: {"description": "Not available outside dev/local/test (or unset)"},
    },
)
async def ddb_rows(
    owner_id: str,
    user_id: str | None = None,
    auth: AuthContext = Depends(get_current_user),
):
    assert_debug_enabled("ddb-rows")

    # Restrict to caller-owned data. The endpoint is meant for the test
    # harness to verify ITS OWN teardown — anyone passing a different
    # owner/user is fishing for cross-tenant info in dev/staging.
    if owner_id != resolve_owner_id(auth):
        raise HTTPException(status_code=403, detail="Cannot query another owner's row counts")
    if user_id is not None and user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="Cannot query another user's row counts")

    # `users` and `ws-connections` are keyed by Clerk user_id even when other
    # tables are scoped by org_id. Caller supplies user_id explicitly in org
    # mode; defaults to owner_id for personal mode where they're the same
    # value.
    effective_user_id = user_id or owner_id

    user = await user_repo.get_by_user_id(effective_user_id)
    container = await container_repo.get_by_owner_id(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    usage = await usage_repo.count_for_owner(owner_id)
    updates = await update_repo.count_for_owner(owner_id)
    channels = await channel_link_repo.count_for_owner(owner_id)
    ws_conns = await connection_service.count_for_user(effective_user_id)

    return {
        "tables": {
            "users": 1 if user else 0,
            "containers": 1 if container else 0,
            "billing-accounts": 1 if billing else 0,
            "usage-counters": usage,
            "pending-updates": updates,
            "channel-links": channels,
            "ws-connections": ws_conns,
        }
    }
