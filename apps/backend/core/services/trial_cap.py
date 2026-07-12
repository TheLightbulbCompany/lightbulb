"""Free-tier $3/day cost cap — the read side.

The **counter is written by the Bedrock invocation-log meter Lambda**
(``apps/infra/lambda/bedrock-usage-meter/``), which sees every model call at
the AWS layer — webchat, cron, channels, Paperclip/Teams runs with no browser
tab, memory compaction, embeddings, the raw RPC proxy — with AWS-sourced,
cache-inclusive token counts, deduped on Bedrock's ``requestId``. No backend
path writes to it. That makes the counter complete by construction and means the
backend never needs a pre-send reserve to bound in-flight spend.

What survives here are read-only primitives: ``is_over_daily_cap`` (what
``gate_chat`` Layer 2, the Teams BFF guards and the chat-steer path all call),
``trial_block_reason`` (the container-start chokepoint's single check), and
``reason_for_account`` (the same predicate for a caller that already holds the
billing row). Blocking on any of these is UX — the authoritative stop is the
meter's revoke-AIP + ``desiredCount=0`` pairing. The INVERSE — giving a
capped-out trial owner their Bedrock access back once the day rolls over —
lives one layer up, in ``core.services.provision_gate.maybe_restore_trial_bedrock``,
which reads ``reason_for_account`` to decide whether it may fire.
"""

import time
from datetime import datetime, timezone

from core.repositories import billing_repo

CAP_MICROCENTS = 3_000_000  # $3.00/day

# The two ways a free-tier owner runs out of runway. Returned by
# ``trial_block_reason`` and mapped to the matching blocked-gate payload by
# ``provision_gate`` — the codes the frontend already renders limit cards for.
TRIAL_EXPIRED = "trial_expired"
DAILY_CAP = "meta_daily_cap"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def is_over_daily_cap(owner_id: str) -> bool:
    """Read-only check: has ``owner_id`` — a ``trial_local`` owner — already
    spent >= ``CAP_MICROCENTS`` today?

    Gated on ``subscription_status == "trial_local"``: paid owners are never
    subject to the free-tier cap, and an owner with no billing row is not a
    trial owner either, so both return False.

    The DDB attribute stays ``meta_daily_spend_microcents`` — a historical
    name kept deliberately (live dev rows carry it); it is now simply the
    trial daily-spend counter, and the meter Lambda is its sole writer.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or account.get("subscription_status") != "trial_local":
        return False
    spent = await billing_repo.get_meta_daily_spend(owner_id, utc_today())
    return spent >= CAP_MICROCENTS


def _is_expired(account: dict) -> bool:
    """Has this trial_local owner's 3-day window elapsed?

    ``trial_local``'s clock is OURS by construction — ``trial_end`` is stamped
    locally at signup (created + 3 days), never sourced from a Stripe
    Subscription. The "Stripe owns the trial clock" rule applies to Stripe
    trials ("trialing"), not this local one, so a plain read-time comparison is
    correct: no persisted status flip, no worker.

    A MISSING ``trial_end`` is not expired — legacy dev rows predate the stamp,
    and the daily cap still bounds them.
    """
    trial_end = account.get("trial_end")
    return trial_end is not None and int(time.time()) > int(trial_end)


async def trial_block_reason(owner_id: str) -> str | None:
    """Why this owner may not have a container running right now, or None.

    ONE billing read, and the single source of truth for both the provision
    gate and ``ecs_manager.start_user_service`` — so the two can never disagree
    about whether a trial owner is allowed a container. Without this, an
    EXPIRED trial could be woken through a start path that only checked the cap.

    Returns ``TRIAL_EXPIRED`` (window elapsed), ``DAILY_CAP`` (over $3 today),
    or None. Paid owners and owners with no billing row always return None —
    neither is subject to the free tier's limits.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    return await reason_for_account(owner_id, account)


async def reason_for_account(owner_id: str, account: dict | None) -> str | None:
    """``trial_block_reason``'s predicate over an ALREADY-FETCHED billing
    account.

    Exists for a caller that read the billing row for its own reason (e.g.
    the daily Bedrock-restore trigger, which must confirm
    ``subscription_status == "trial_local"`` before it may restore) and would
    otherwise pay for a second GetItem of the same row just to re-derive this
    same answer. ``trial_block_reason`` delegates here after its own read, so
    the two can never diverge.
    """
    if not account or account.get("subscription_status") != "trial_local":
        return None
    if _is_expired(account):
        return TRIAL_EXPIRED
    spent = await billing_repo.get_meta_daily_spend(owner_id, utc_today())
    if spent >= CAP_MICROCENTS:
        return DAILY_CAP
    return None
