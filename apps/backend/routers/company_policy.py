"""Owner capability policy + revoke-all — /api/v1/company/{policy,revoke-all}.

The P0d slice: this router ships the policy ROW and its owner surface only.
The map (`money_moves`/`outreach`/`publishing`/`provisioning` → "requires
owner approval?", absent ⇒ True, fail-closed) is deliberately NOT enforced
inside any rail yet — enforcement wiring is P1.

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
from core.repositories import billing_repo, integrations_repo
from core.services import account_wipe, company_stripe
from core.services.credentials import integration_secret_store
from core.services.owner_facts import write_owner_facts

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


@router.patch("/policy")
async def patch_policy(body: dict[str, bool], auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = _owner(auth)
    try:
        updated = await billing_repo.set_capability_policy(owner_id, body)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if updated is None:
        raise HTTPException(status_code=404, detail="No billing account")
    return updated


class RevokeAllBody(BaseModel):
    confirm: str = ""


@router.post("/revoke-all")
async def revoke_all(body: RevokeAllBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """The kill switch: close open provisioning asks, delete OUR credential
    copies, and sever the Stripe Connect link. Order matters — workflows
    first, so no reader mid-step can land a fresh secret after the deletes.
    Every step is idempotent; a raise leaves earlier steps done and the call
    safe to retry."""
    owner_id = _owner(auth)
    if body.confirm != "revoke":
        raise HTTPException(status_code=422, detail='confirm must be exactly "revoke"')

    revoked: list[str] = []

    # 1. Cancel open provisioning workflows (pure DDB; never deprovisions).
    cancelled, _open = await account_wipe.cancel_open_workflows(owner_id)
    revoked.append(f"workflows_cancelled:{cancelled}")

    # 2. Delete our Secrets Manager copies: every projects/* credential the
    # Provisioning rail landed, plus the retained posthog token (delete
    # no-ops when absent).
    keys = [
        r["provider"]
        for r in await integrations_repo.list_for_owner(owner_id)
        if str(r.get("provider", "")).startswith(integrations_repo.PROJECTS_KEY_PREFIX)
    ]
    keys.append("posthog")
    for key in keys:
        await integration_secret_store.delete(owner_id, key)
        revoked.append(f"secret:{key}")

    # 3. Stripe Connect: the disconnect route's service path — sever the
    # platform link and unbind. The owner's account itself is untouched.
    # `release_and_unbind` unbinds whatever Stripe answers: this is a kill
    # switch, and one that 500s on a Stripe outage (or on a `created` account
    # Stripe declines to deauthorize) is not a kill switch. An unconfirmed
    # revoke is reported in the payload rather than raised.
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if account_id:
        released = await company_stripe.release_and_unbind(
            owner_id=owner_id, account_id=account_id, source=row.get("company_stripe_source")
        )
        revoked.append(f"stripe_connect:{account_id}" + ("" if released else "(stripe_unconfirmed)"))
        await write_owner_facts(owner_id)

    logger.info("company_policy: revoke-all owner_id=%s revoked=%s", owner_id, revoked)
    return {"revoked": revoked}
