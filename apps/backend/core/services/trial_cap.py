"""Free-tier $3/day cost cap — the read side.

The **counter is written by the Bedrock invocation-log meter Lambda**
(``apps/infra/lambda/bedrock-usage-meter/``), which sees every model call at
the AWS layer — webchat, cron, channels, Paperclip/Teams runs with no browser
tab, memory compaction, embeddings, the raw RPC proxy — with AWS-sourced,
cache-inclusive token counts, deduped on Bedrock's ``requestId``. No backend
path writes to it. That makes the counter complete by construction and means the
backend never needs a pre-send reserve to bound in-flight spend.

What survives here are read-only primitives: ``trial_block_reason`` (ONE
billing read — the single check used by gate_chat Layer 2, the container-start
chokepoint, /container/recover, the container-wake path, the chat-steer path and
the Teams BFF run guards) and ``reason_for_account`` (the same predicate for a
caller that already holds the billing row).

There is deliberately NO cap-only primitive. One used to exist, and four
separate doors reached for it and so checked only HALF the predicate — happily
waking or dispatching work for a trial owner whose window had EXPIRED but who
was under today's $3. Every door must answer the whole question. Blocking on
any of these is UX. The PRIMARY enforcement is the bedrock-gate's synchronous
pre-flight reservation (admission refused before a token is generated); the
meter's kill (deny the gate token + ``StopRuntimeSession`` + the now-vestigial
IAM revoke) is the ASYNC BACKSTOP if the counter ever crosses the cap anyway. The
INVERSE — giving a capped-out trial owner their access back once the day rolls
over — lives one layer up, in
``core.services.provision_gate.maybe_restore_trial_bedrock``, which reads
``reason_for_account`` to decide whether it may fire.
"""

import time
from datetime import datetime, timezone

from core.repositories import billing_repo

# A TRUE $3.00/day. The bedrock-gate's synchronous pre-flight reservation is
# the primary enforcement (admission refused before a token is generated;
# overshoot bounded to in-flight requests), so the old $2.60 enforce-early
# margin — which compensated for the async meter's 30-90s log lag — is
# retired. Lockstep with the meter Lambda's copy in
# apps/infra/lambda/bedrock-usage-meter/index.py AND the gate stack's
# CAP_MICROCENTS env (bedrock-gate-stack.ts) — the gate's admission, the
# meter's backstop kill, and the restore eligibility all read the same
# number; moving one alone desynchronizes kill/restore/admission.
CAP_MICROCENTS = 3_000_000

# The bedrock-gate's per-request reservation hold (apps/infra/lambda/
# bedrock-gate/ledger.mjs). Admission is `spent + reserved + estimate <= cap`,
# so the LAST admissible spend is CAP - RESERVE — not CAP. Every predicate that
# claims to answer "will the next request be admitted?" must subtract it, or it
# opens a dead zone where the backend forwards a turn the gate then refuses.
#
# Lockstep with RESERVE_ESTIMATE_MICROCENTS in
# apps/infra/lib/stacks/bedrock-gate-stack.ts — moving one alone desynchronizes
# admission from every door that predicts it.
RESERVE_ESTIMATE_MICROCENTS = 300_000

# The two ways a free-tier owner runs out of runway. Returned by
# ``trial_block_reason`` and mapped to the matching blocked-gate payload by
# ``provision_gate`` — the codes the frontend already renders limit cards for.
TRIAL_EXPIRED = "trial_expired"
DAILY_CAP = "trial_daily_cap"


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _is_expired(account: dict) -> bool:
    """Has this trial_local owner's 30-day window elapsed?

    ``trial_local``'s clock is OURS by construction — ``trial_end`` is armed
    locally when provisioning durably reaches ready (ready + 30 days), never sourced
    from a Stripe Subscription. The "Stripe owns the trial clock" rule applies to Stripe
    trials ("trialing"), not this local one, so a plain read-time comparison is
    correct: no persisted status flip, no worker.

    A MISSING ``trial_end`` is not expired — the trial remains unarmed until
    its workspace runtime first becomes ready, and the daily cap still bounds it.
    """
    trial_end = account.get("trial_end")
    return trial_end is not None and int(time.time()) >= int(trial_end)


def is_trial_expired(account: dict | None) -> bool:
    """Public: True only for a ``trial_local`` owner whose 30-day window elapsed.

    Expiry ONLY — the daily $3 cap is a separate, transient gate that resets at
    the UTC rollover, so it is deliberately not folded in here. Nightly-CEO
    arming keys off expiry (a capped-today trial is still armed; the cap gates
    the run, not the arm)."""
    return bool(account) and account.get("subscription_status") == "trial_local" and _is_expired(account)


async def trial_block_reason(owner_id: str) -> str | None:
    """Why this owner may not have a container running right now, or None.

    ONE billing read, and the single source of truth for both the provision
    gate and every other door — so they can never disagree
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
    from core.services.container_provision import provider_choice_from_row

    if provider_choice_from_row(account)[0] == "opencode_zen":
        return None
    spent = await billing_repo.get_trial_daily_spend(owner_id, utc_today())
    # `>` (not `>=`) mirrors the gate's own inequality exactly, so spend of
    # precisely CAP - RESERVE still admits. `reserved` is deliberately NOT read
    # here: holds are transient and released on settle, so folding them in would
    # over-block an owner with two runs legitimately in flight. `spent` is the
    # durable floor — and it is what makes the refusal band permanent, which is
    # the bug this closes. A turn that crosses the line mid-run is caught by the
    # gateway's reclassification instead.
    if spent + RESERVE_ESTIMATE_MICROCENTS > CAP_MICROCENTS:
        return DAILY_CAP
    return None
