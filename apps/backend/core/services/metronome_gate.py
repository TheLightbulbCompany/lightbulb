"""Synchronous credits gate over the Metronome fast-gate projection.

The chat gate (``connection_pool.gate_chat``) and the provision gate both
ask one question — "is this Bedrock-managed owner out of credits?" — and
must answer it without a synchronous Metronome round-trip on every message
(Metronome's balance read has no low-latency SLA). Both read THIS helper so
they can never disagree about blocked-ness.

Read model:
- The fast-gate DDB row (fed by the low-balance webhook) is the cached
  answer. ``remaining_microcents <= RESERVE_ESTIMATE_MICROCENTS`` blocks —
  NOT ``<= 0`` — mirroring the bedrock-gate's own admission rule (see
  ``_row_blocked``); a MISSING row does NOT block (a brand-new owner must
  not be gated by a missing projection).
- When the row is stale (older than ``METRONOME_GATE_STALENESS_S``) and the
  owner actually has a Metronome customer, the gate lazily refreshes from
  ``getNetBalance`` and rewrites the row. Metronome-down ⇒ the stale row is
  used as-is — the gate must never 500 (or block) on a vendor outage.
- An owner with NO Metronome customer ⇒ BLOCK. "No ledger" and "vendor
  unreachable" both surface as an unavailable refresh but mean opposite
  things, and treating the first as "not blocked" is unmetered spend on our
  account, not leniency. See ``is_owner_blocked`` for the full argument.
- A refresh that comes back POSITIVE for an owner whose containers row says
  ``bedrock_revoked`` un-revokes (``restore_owner_bedrock``) after ONE
  confirming re-read. This is the pull-based re-arm: Metronome's
  payment-gated top-up releases credits with NO webhook to us, so the next
  gate read after a paid top-up is the event that restores Bedrock access.
- A refresh that comes back NON-POSITIVE re-asserts the revoke
  (``enforce_on_low_balance``): the low-balance webhook is one-shot and can
  be lost, so every gate read doubles as the re-enforce loop.
"""

from __future__ import annotations

import logging

import httpx

from core.billing.metronome_fast_gate import MetronomeFastGate, is_stale
from core.billing.metronome_units import cents_to_microcents
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services.bedrock_enforcement import enforce_on_low_balance
from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

logger = logging.getLogger(__name__)


async def is_owner_blocked(owner_id: str) -> bool:
    """True when the owner is out of spendable Bedrock credits.

    The single blocked-ness predicate for both the pre-chat gate and the
    provision gate. Never raises on Metronome/DDB trouble in the refresh
    path — the cached row (or, failing that, "not blocked") is the answer.
    """
    gate = MetronomeFastGate()
    row = await gate.get_row(owner_id)
    if not is_stale(row, settings.METRONOME_GATE_STALENESS_S):
        return _row_blocked(row)

    refreshed = await _refresh_from_metronome(owner_id, gate)
    if refreshed is not None:
        return refreshed <= RESERVE_ESTIMATE_MICROCENTS

    # Refresh unavailable. That is TWO different situations and they get
    # opposite answers — conflating them is what let a paid Bedrock owner run
    # uncredited:
    #
    #   no Metronome customer -> BLOCK. The owner has no ledger, so no credits
    #     can exist and none can ever be spent down to zero. Every caller of
    #     this predicate has already established provider_choice ==
    #     "bedrock_claude" (see the three call sites), so this is an owner
    #     billing inference to US with nothing backing it. Answering "not
    #     blocked" here handed them unmetered Bedrock — Opus-tier for the CEO
    #     agent — on our account, indefinitely. Observed on prod 2026-08-06.
    #     They see the `credits_required` card, and the top-up path re-ensures
    #     provisioning, so this is a prompt to pay rather than a dead end.
    #
    #   customer exists, Metronome unreachable -> fall back to the stale row.
    #     A vendor outage must never lock out a customer who has already paid;
    #     their real balance is whatever the cache last saw.
    if not await _has_metronome_customer(owner_id):
        put_metric("metronome.gate.blocked_unprovisioned")
        logger.warning(
            "Owner %s is on bedrock_claude with no Metronome customer — blocking. "
            "Provisioning should have run on the entering_live webhook; the top-up "
            "path re-ensures it.",
            owner_id,
        )
        return True
    return _row_blocked(row)


async def _has_metronome_customer(owner_id: str) -> bool:
    """Whether the owner has a Metronome ledger at all.

    Deliberately a separate read rather than a signal threaded out of
    ``read_net_balance_microcents``: this runs only on the cold path (stale
    row AND refresh unavailable), and for an owner in that state the answer
    is about to block them, so the read is self-limiting. A DDB failure
    reports "has a customer" so a transient blip degrades to the stale-row
    fallback rather than locking out a paying customer.
    """
    try:
        account = await billing_repo.get_by_owner_id(owner_id)
    except Exception:
        logger.exception("Billing read failed while checking Metronome provisioning for owner %s", owner_id)
        return True
    return bool((account or {}).get("metronome_customer_id"))


def _row_blocked(row: dict | None) -> bool:
    if not row:
        return False
    remaining = row.get("remaining_microcents")
    if remaining is None:
        return bool(row.get("blocked", False))
    # The gate admits only while `remaining - reserved - estimate > 0`, so a
    # balance at or below one reservation is already unspendable. Matching it
    # here keeps the banner, the chat gate and the provision gate agreeing with
    # the only component that actually decides.
    return int(remaining) <= RESERVE_ESTIMATE_MICROCENTS


async def read_net_balance_microcents(owner_id: str) -> int | None:
    """One live ``getNetBalance`` read for the owner, in microcents.

    Returns None when the owner has no Metronome customer or the response
    carries no balance number. Raises on transport/HTTP failure — each
    caller picks its own fallback (the gate falls back to the cached row,
    the balance endpoint degrades to null).
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    customer_id = (account or {}).get("metronome_customer_id")
    if not customer_id:
        return None

    from core.billing import metronome_client

    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=5.0) as http:
        data = await metronome_client.for_settings(http).get_net_balance(customer_id=customer_id)
    # Live-verified response shape (sandbox, 2026-06-10):
    #   {"data": {"balance": <number-in-CENTS>, "credit_type_id": …}}
    # The client returns the ``data`` sub-object, so the number lives under
    # "balance" — NOT "net_balance". Metronome's USD credit type is
    # cents-denominated (see core/billing/metronome_units.py); the gate and
    # the API surface speak microcents.
    balance_cents = data.get("balance")
    if balance_cents is None:
        return None
    return cents_to_microcents(float(balance_cents))


async def apply_fresh_balance(
    owner_id: str, remaining_microcents: int, *, gate: MetronomeFastGate | None = None
) -> None:
    """The single "a fresh Metronome balance just arrived" hook.

    Shared by the gate's lazy refresh and the credits-balance endpoint
    write-through, so the cache rewrite, the enforcement re-assert, and the
    pull-based restore can never diverge between the two read paths.
    Best-effort throughout — must never raise into a gate read or the
    balance endpoint.
    """
    if gate is None:
        gate = MetronomeFastGate()
    try:
        await gate.set_balance(owner_id, remaining_microcents=remaining_microcents)
    except Exception:
        # The fresh number still answers THIS read; the row rewrite is only
        # the cache for the next one.
        logger.warning("Fast-gate rewrite failed for owner %s after refresh", owner_id, exc_info=True)

    if remaining_microcents > 0:
        await _maybe_restore_bedrock(owner_id)
        return

    # Re-assert the revoke on every fresh non-positive read. The low-balance
    # webhook is a ONE-SHOT signal that can be eaten (stale-skip racing a
    # redelivery) or undone (a stale-positive refresh restoring a
    # truly-drained owner), and a blocked owner emits no usage so no
    # corrective webhook ever comes — the gate's own reads are the only
    # recurring trigger left. enforce_on_low_balance is idempotent on the
    # bedrock_revoked stamp, so the common already-revoked case costs one
    # DDB read.
    try:
        await enforce_on_low_balance(owner_id, remaining_microcents)
    except Exception:
        put_metric("billing.bedrock_revoke.error")
        logger.exception(
            "Re-assert revoke failed for owner %s — the fast-gate row already blocks new sends",
            owner_id,
        )


async def _refresh_from_metronome(owner_id: str, gate: MetronomeFastGate) -> int | None:
    """Pull ``getNetBalance`` and run the shared fresh-balance hook (cache
    rewrite + enforcement re-assert + pull-based restore). Returns the
    refreshed remaining (microcents), or None when no refresh was possible.
    """
    try:
        remaining_microcents = await read_net_balance_microcents(owner_id)
    except Exception:
        put_metric("metronome.gate.refresh_error")
        logger.warning(
            "Metronome gate refresh failed for owner %s — using cached fast-gate row",
            owner_id,
            exc_info=True,
        )
        return None
    if remaining_microcents is None:
        return None

    await apply_fresh_balance(owner_id, remaining_microcents, gate=gate)
    return remaining_microcents


async def _maybe_restore_bedrock(owner_id: str) -> None:
    """Pull-based re-arm: un-revoke a paid-up owner. Best-effort — a restore
    failure must not block (or 500) the gate read; the owner stays revoked
    until the next refresh re-attempts. Lazy imports keep the IAM/container
    layer out of this module's import tree.
    """
    from core.repositories import container_repo

    try:
        container = await container_repo.get_by_owner_id(owner_id)
        if not container or not container.get("bedrock_revoked"):
            return
        # Confirm with ONE fresh getNetBalance before un-revoking.
        # getNetBalance is async with no SLA, so a single stale-positive
        # read against a truly-drained owner would restore Bedrock AND
        # permanently disarm the one-shot low-balance alert (it already
        # fired for this crossing) — nothing would ever re-revoke. Only a
        # fresh read that still says > 0 may restore.
        confirmed = await read_net_balance_microcents(owner_id)
        if confirmed is None or confirmed <= 0:
            put_metric("billing.bedrock_restore.confirm_failed")
            logger.warning(
                "Bedrock restore for owner %s aborted — confirming balance re-read came back %s",
                owner_id,
                confirmed,
            )
            return
        from core.billing.bedrock_aip import restore_owner_bedrock

        put_metric("billing.bedrock_restore.fire", dimensions={"reason": "gate_refresh_positive"})
        logger.warning("Owner %s paid up (balance > 0) — restoring Bedrock IAM grant", owner_id)
        await restore_owner_bedrock(owner_id)
    except Exception:
        put_metric("billing.bedrock_restore.error")
        logger.exception(
            "Bedrock restore failed for owner %s — next gate refresh re-attempts",
            owner_id,
        )
