"""The free-tier / capped-plan block reasons — read-only.

``trial_block_reason`` (ONE billing read — the single check used by gate_chat Layer 2, the
container-start chokepoint, /container/recover, the container-wake path, the chat-steer path and
the Teams BFF run guards) and ``reason_for_account`` (the same predicate for a caller that already
holds the billing row).

A capped owner is blocked by exactly two things: an expired ``trial_local`` window, and
``openrouter_capped_day`` — stamped by the openrouter-gate when OpenRouter's per-key daily limit
refuses the owner, and self-clearing at the UTC rollover. The OpenRouter key limit is the valve;
these reads are UX (the limit card instead of a failed run).

There is deliberately NO cap-only primitive. One used to exist, and four separate doors reached
for it and so checked only HALF the predicate — waking work for a trial owner whose window had
EXPIRED. Every door must answer the whole question.
"""

import time
from datetime import datetime, timezone

from core.repositories import billing_repo

# The two ways a free-tier owner runs out of runway. Returned by
# ``trial_block_reason`` and mapped to the matching blocked-gate payload by
# ``provision_gate`` — the codes the frontend already renders limit cards for.
TRIAL_EXPIRED = "trial_expired"
DAILY_CAP = "trial_daily_cap"
# A PAYING capped owner (Base, or Workspace/Ultra with no provider) at OpenRouter's per-key daily
# limit. Its own code so no surface shows them the free-trial card (owner ruling 2026-09-22).
INCLUDED_CAP = "included_daily_cap"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_expired(account: dict) -> bool:
    """Has this trial_local owner's 30-day window elapsed?

    ``trial_local``'s clock is OURS by construction — ``trial_end`` is armed
    locally on the first canonical ready poll (ready + 30 days), never sourced
    from a Stripe Subscription. The "Stripe owns the trial clock" rule applies to Stripe
    trials ("trialing"), not this local one, so a plain read-time comparison is
    correct: no persisted status flip, no worker.

    A MISSING ``trial_end`` is not expired — the trial remains unarmed until
    its workspace runtime first becomes ready, and the daily key limit still bounds it.
    """
    trial_end = account.get("trial_end")
    return trial_end is not None and int(time.time()) > int(trial_end)


def _paid_voice(account: dict | None) -> bool:
    from core.billing.voice_billing import has_voice_plan

    return has_voice_plan(account)


def is_trial_expired(account: dict | None) -> bool:
    """True for an expired local trial without currently collected voice coverage.

    Expiry ONLY — the daily cap is a separate, transient gate that resets at
    the UTC rollover, so it is deliberately not folded in here. Nightly-CEO
    arming keys off expiry (a capped-today trial is still armed; the cap gates
    the run, not the arm)."""
    return (
        bool(account)
        and not _paid_voice(account)
        and account.get("subscription_status") == "trial_local"
        and _is_expired(account)
    )


async def trial_block_reason(owner_id: str) -> str | None:
    """Why this owner may not have a container running right now, or None.

    ONE billing read, and the single source of truth for both the provision
    gate and every other door — so they can never disagree
    about whether a trial owner is allowed a container. Without this, an
    EXPIRED trial could be woken through a start path that only checked the cap.

    Returns ``TRIAL_EXPIRED`` (window elapsed), ``DAILY_CAP`` / ``INCLUDED_CAP``
    (OpenRouter's daily key limit hit today), or None. Paid owners and owners with no billing row always return None —
    neither is subject to the free tier's limits.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    return await reason_for_account(owner_id, account)


def is_managed_capped(account: dict | None) -> bool:
    """True for an owner on the cheap managed model whose OpenRouter per-key
    daily $ limit is the valve: a trial_local row, a paid voice plan with no
    explicit provider, a Base row, or (interim) a
    paid Workspace/Ultra row that never stored a provider_choice. Lockstep with
    the openrouter-gate's ``capped`` predicate (apps/infra/lambda/openrouter-gate/core.mjs)."""
    if not account:
        return False
    if _paid_voice(account):
        return not account.get("provider_choice")
    status = account.get("subscription_status")
    if status == "trial_local":
        return True
    live = status in {"active", "trialing"}
    if account.get("plan") == "base":
        return live
    return account.get("plan") in {"standard", "ultra_pro"} and not account.get("provider_choice") and live


async def reason_for_account(owner_id: str, account: dict | None) -> str | None:
    """``trial_block_reason``'s predicate over an ALREADY-FETCHED billing
    account.

    Exists for a caller that read the billing row for its own reason and would
    otherwise pay for a second GetItem of the same row just to re-derive this
    same answer. ``trial_block_reason`` delegates here after its own read, so
    the two can never diverge.
    """
    if not is_managed_capped(account):
        return None
    trial = account.get("subscription_status") == "trial_local" and not _paid_voice(account)
    if trial and _is_expired(account):
        return TRIAL_EXPIRED
    # The openrouter-gate stamps this when OpenRouter's per-key daily limit
    # 402s a capped owner; it clears itself at the UTC rollover.
    if account.get("openrouter_capped_day") == utc_today():
        return DAILY_CAP if trial else INCLUDED_CAP
    return None
