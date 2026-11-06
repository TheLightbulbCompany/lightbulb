"""Owner capability policy, its Always-allow rules + revoke-all — /api/v1/company/{policy,policy/always-allow,revoke-all}.

The map (`money_moves`/`outreach`/`publishing`/`provisioning` → "requires
owner approval?", absent ⇒ True, fail-closed) plus `approval_mode` (ask | auto | never_ask, absent ⇒ auto). The
mode is read by the super-agent effects route (`runtime_internal.effect_check`); `outreach`/`money_moves` pick who
answers a card; `publishing`/`provisioning` have no reader.

Gating copies routers/company_provisioning.py's owner-route admin check: org
context requires org:admin, and the owner is the org, never the member who
clicked. Deliberately NOT paid-gated — revoking access must keep working for
a lapsed subscription.

Revoke-all composes EXISTING primitives and NEVER touches provider resources
themselves (spec §3.6: teardown never deprovisions).
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.repositories import billing_repo, container_repo, integrations_repo, slack_installs_repo
from core.services import account_wipe, company_stripe, link_wallet, questions, slack_install_service, website_logins
from core.services.credentials import integration_secret_store
from core.services.owner_facts import write_owner_facts
from routers import connections

logger = logging.getLogger(__name__)
router = APIRouter()


def _owner(auth: AuthContext) -> str:
    if auth.is_org_context:
        require_org_admin(auth)
    return resolve_owner_id(auth)


@router.get("/policy")
async def get_policy(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """The filled map — every key present, absent-in-storage ⇒ True."""
    return await billing_repo.get_capability_policy(_owner(auth))


#: The typed word that turns on Never ask (admin-only, like "revoke"): checked here, not only in the UI.
NEVER_ASK_CONFIRM = "never ask"


@router.patch("/policy")
async def patch_policy(body: dict[str, bool | str], auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = _owner(auth)
    confirm = body.pop("confirm", None)
    if body.get(billing_repo.APPROVAL_MODE_KEY) == "never_ask" and confirm != NEVER_ASK_CONFIRM:
        raise HTTPException(status_code=422, detail=f'Never ask needs confirm exactly "{NEVER_ASK_CONFIRM}"')
    try:
        updated = await billing_repo.set_capability_policy(owner_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="No billing account")
    if billing_repo.APPROVAL_MODE_KEY in body:
        await questions.audit(auth.user_id, owner_id, "approval.mode", {"mode": body[billing_repo.APPROVAL_MODE_KEY]})
    return updated


@router.get("/policy/always-allow")
async def list_always_allow(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Settings › Approvals: every Always-allow rule a decision card wrote (questions.add_always_allow)."""
    return {"rules": await questions.always_allow_rules(_owner(auth))}


@router.delete("/policy/always-allow/{rule_id}")
async def remove_always_allow(rule_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Remove one: that action asks again. 404 when it is already gone; 409 when the rules kept changing."""
    owner_id = _owner(auth)
    try:
        removed = await questions.remove_always_allow(owner_id, rule_id, by=auth.user_id)
    except questions.RulesBusy:
        raise HTTPException(status_code=409, detail="busy") from None
    if not removed:
        raise HTTPException(status_code=404, detail="No such rule")
    return {"removed": rule_id}


class RevokeAllBody(BaseModel):
    confirm: str = ""


@router.post("/revoke-all")
async def revoke_all(body: RevokeAllBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """The kill switch: close open provisioning asks, delete OUR credential
    copies, disconnect every connected app, and sever the Stripe Connect link. Order matters — workflows
    first, so no reader mid-step can land a fresh secret after the deletes.
    Every step is idempotent; a raise leaves earlier steps done and the call
    safe to retry."""
    owner_id = _owner(auth)
    if body.confirm != "revoke":
        raise HTTPException(status_code=422, detail='confirm must be exactly "revoke"')

    revoked: list[str] = []
    failed: list[str] = []

    # 1. Cancel open provisioning workflows (pure DDB; never deprovisions).
    cancelled, _open = await account_wipe.cancel_open_workflows(owner_id)
    if cancelled:
        revoked.append(f"workflows_cancelled:{cancelled}")

    # 2. Delete our Secrets Manager copies: every projects/* credential the
    # Provisioning rail landed, plus the retained posthog token. Only what was
    # really there is reported: the toast counts this list.
    # Mark first: every OAuth connect started before now is refused when it lands
    # (connections.oauth_callback). Then read the rows under the lease, so a callback
    # already past that check finishes its write before the snapshot and is torn down below.
    await container_repo.mark_oauth_revoked(owner_id)
    try:
        async with connections.owner_lifecycle_guard(owner_id):
            items = await integrations_repo.list_for_owner(owner_id, consistent=True)
            rows = [str(r.get("provider", "")) for r in items]
            # The owner's slack row (read strongly) names every team installed, including one the
            # by-owner GSI has not caught up with; the GSI covers installs from before that row
            # carried team_ids.
            teams = sorted(
                {
                    r["team_id"]
                    for r in await slack_installs_repo.list_for_owner(owner_id)
                    if r.get("status") == slack_installs_repo.STATUS_ACTIVE
                }
                | {t for r in items if r.get("provider") == "slack" for t in r.get("team_ids") or ()}
            )
            billing_row = await billing_repo.get_by_owner_id(owner_id) or {}
    except connections.OwnerLifecycleBusyError:
        raise HTTPException(status_code=409, detail="busy") from None
    keys = [k for k in rows if k.startswith(integrations_repo.PROJECTS_KEY_PREFIX)]
    keys.append("posthog")
    for key in keys:
        if await integration_secret_store.delete(owner_id, key):
            revoked.append(f"secret:{key}")

    # 2b. Every connected app, through the Disconnect route's own teardown (same per-owner
    # guard, same github follow-up). One that fails is reported; the rest still go.
    for provider in rows:
        if provider.startswith(integrations_repo.PROJECTS_KEY_PREFIX):
            continue
        try:
            p = connections._get_provider_for_teardown(provider)
        except HTTPException:
            continue  # a reserved key on the same table (website_logins, manual_mcp), not an app
        try:
            async with connections.owner_lifecycle_guard(owner_id):
                await connections._disconnect_teardown(owner_id, provider, p)
            if provider == "github":
                await connections.paperclip_github_token.sync_after_disconnect(owner_id)
            revoked.append(f"connection:{provider}")
        except Exception:  # noqa: BLE001 — a kill switch keeps going past one bad provider
            logger.exception("company_policy: revoke-all could not disconnect %s owner_id=%s", provider, owner_id)
            failed.append(provider)

    # 2c. Slack is not a registry provider (its bare row is skipped above): uninstall each
    # workspace through the Disconnect route's revoke_install (row, slack/<team> token, routes).
    slack_failed = False
    for team_id in teams:
        try:
            # Only a workspace that is ours right now (the check DELETE /slack/install/{team} makes):
            # a team id we once held may have been installed by another owner since.
            row = await slack_installs_repo.get(team_id)
            if not row or row.get("owner_id") != owner_id or row.get("status") != slack_installs_repo.STATUS_ACTIVE:
                continue
            if not await slack_install_service.revoke_install(team_id, expected_owner_id=owner_id):
                continue  # it changed hands after the check above: not ours to touch
            revoked.append(f"slack:{team_id}")
        except Exception:  # noqa: BLE001 — same as above: report it, keep going
            logger.exception(
                "company_policy: revoke-all could not uninstall slack team=%s owner_id=%s", team_id, owner_id
            )
            slack_failed = True
    if slack_failed:
        failed.append("slack")

    # 2d. Saved website logins: each one's password is a credential copy we hold. Same as
    # above: a failure is reported and the rest of the kill switch still runs.
    try:
        logins = await website_logins.list_logins(owner_id)
    except Exception:  # noqa: BLE001 — report it, keep going
        logger.exception("company_policy: revoke-all could not list website logins owner_id=%s", owner_id)
        logins = []
        failed.append("website_logins")
    for login in logins:
        try:
            if await website_logins.delete_login(owner_id, login["id"], actor=auth.user_id):
                revoked.append(f"website_login:{login['id']}")
        except Exception:  # noqa: BLE001 — one bad login must not spare the rest
            logger.exception(
                "company_policy: revoke-all could not delete website login %s owner_id=%s", login["id"], owner_id
            )
            failed.append(f"website_login:{login['id']}")

    # 2e. The Link wallet: its tokens are a credential copy we hold, and the grant is the owner's
    # own Link account. Revoked at Link, then deleted (runs whether or not the feature flag is on).
    try:
        if await link_wallet.disconnect(owner_id):
            revoked.append("link_wallet")
    except Exception:  # noqa: BLE001 -- report it, keep going
        logger.exception("company_policy: revoke-all could not disconnect the link wallet owner_id=%s", owner_id)
        failed.append("link_wallet")

    # 3. Stripe Connect: the disconnect route's service path — sever the
    # platform link and unbind. The owner's account itself is untouched.
    # `release_and_unbind` unbinds whatever Stripe answers: this is a kill
    # switch, and one that 500s on a Stripe outage (or on a `created` account
    # Stripe declines to deauthorize) is not a kill switch. An unconfirmed
    # revoke is reported in the payload rather than raised.
    row = billing_row
    account_id = row.get("company_stripe_account_id")
    if account_id:
        released = await company_stripe.release_and_unbind(
            owner_id=owner_id, account_id=account_id, source=row.get("company_stripe_source")
        )
        revoked.append(f"stripe_connect:{account_id}" + ("" if released else "(stripe_unconfirmed)"))
        await write_owner_facts(owner_id)

    logger.info("company_policy: revoke-all owner_id=%s revoked=%s failed=%s", owner_id, revoked, failed)
    return {"revoked": revoked, "failed": failed}
