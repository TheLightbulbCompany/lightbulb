"""Metronome funding for confirmed Stripe top-ups.

A top-up Stripe Checkout payment funds an UNGATED Metronome PREPAID commit:
our own Checkout already collected the money, so a Metronome payment gate
would invoice-and-charge the customer a SECOND time (or never release the
credits). Called from the Stripe webhook (already deduped by event.id), so
this function does not re-dedup; it stamps the Stripe session id into the
commit description for audit.

After the commit lands it un-kills the owner (restore_owner_bedrock) and
optimistically re-arms the local fast-gate cache — both best-effort — so a
previously-out-of-credits owner unblocks on their next message. The
synchronous gate reads the cache, and Metronome fires no recovery webhook on
a top-up, so this is the only re-arm path besides the lazy poll.
"""

from __future__ import annotations

import logging
import time

import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.billing.metronome_units import microcents_to_cents
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# Prepaid credits never expire on our side; the schedule item still needs an
# end, so it gets one far past any plausible product lifetime.
_NO_EXPIRY = "9999-12-31T00:00:00.000Z"


class FundingError(Exception):
    pass


async def fund_top_up(
    *,
    owner_id: str,
    amount_microcents: int,
    product_id: str,
    stripe_session_id: str,
    client: MetronomeClient | None = None,
    metronome_customer_id: str | None = None,
    metronome_contract_id: str | None = None,
) -> None:
    """Add an ungated prepaid commit for amount_microcents.

    The Metronome customer UUID + contract id live on the billing row (where
    ensure_metronome_customer persists them at provision time). Callers that
    already hold the row (e.g. the Stripe webhook, which reads it to decide
    whether the owner is Metronome-provisioned at all) pass both ids through
    to skip the duplicate billing-row read.
    """
    if not settings.METRONOME_LIFECYCLE_ENABLED:
        logger.info("Metronome lifecycle disabled — skipping funding for owner %s", owner_id)
        return
    if amount_microcents <= 0:
        raise FundingError(f"amount_microcents must be positive, got {amount_microcents}")

    if not (metronome_customer_id and metronome_contract_id):
        account = await billing_repo.get_by_owner_id(owner_id) or {}
        metronome_customer_id = metronome_customer_id or account.get("metronome_customer_id")
        metronome_contract_id = metronome_contract_id or account.get("metronome_contract_id")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    commit = {
        # Documented add_commits shape: PREPAID + product + an access
        # schedule whose single item carries the amount, in CENTS (Metronome's
        # USD credit type — see core/billing/metronome_units.py). No
        # payment_gate_config: the money was already collected by our Stripe
        # Checkout. Exact schema is sandbox-validated before cutover (this
        # path ships dormant behind METRONOME_LIFECYCLE_ENABLED).
        "type": "PREPAID",
        "product_id": product_id,
        "access_schedule": {
            "schedule_items": [
                {
                    "amount": microcents_to_cents(amount_microcents),
                    "starting_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "ending_before": _NO_EXPIRY,
                }
            ]
        },
        "description": f"isol8 top-up stripe_session={stripe_session_id} owner={owner_id}",
    }

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await metronome_client.for_settings(http).edit_contract_add_commits(
                metronome_customer_id=metronome_customer_id,
                contract_id=metronome_contract_id,
                add_commits=[commit],
            )
    else:
        await client.edit_contract_add_commits(
            metronome_customer_id=metronome_customer_id,
            contract_id=metronome_contract_id,
            add_commits=[commit],
        )

    # Un-kill: re-attach the per-owner scoped Bedrock policy now that the
    # owner is funded again — the kill-switch may have revoked it when the
    # balance crossed zero, and Metronome fires no recovery webhook to undo
    # it. Idempotent when the kill never fired. Best-effort for the same
    # reason as the re-arm below: the commit already landed against a
    # confirmed payment, so a restore failure must not 500 the webhook back
    # to Stripe (redelivery storm / double commit). It also raises for
    # owners with no persisted AIP map (non-Bedrock providers) — equally a
    # log-and-continue, not a funding failure. Imported lazily to keep the
    # IAM/container layer out of this module's import tree.
    try:
        from core.billing.bedrock_aip import restore_owner_bedrock

        await restore_owner_bedrock(owner_id)
    except Exception:
        logger.exception(
            "Bedrock restore failed after funding owner %s session %s (commit landed; gate re-arm still proceeds)",
            owner_id,
            stripe_session_id,
        )

    # Optimistically re-arm the local fast-gate cache so the owner's NEXT chat
    # message unblocks immediately. get_balance reads the cache (not Metronome's
    # async getNetBalance), and the low-balance webhook only fires when balance
    # crosses DOWN past a threshold — never on a top-up — so without this re-arm
    # a paid top-up would never unblock a previously-out-of-credits owner
    # (spec §3.3: "fund Metronome prepaid credit -> flag refreshed"). Safe
    # direction: the commit above already landed against a CONFIRMED Stripe
    # payment. Best-effort — the commit is what releases money; if the cache
    # write fails the next low-balance webhook re-syncs the authoritative
    # number, so we must not raise (that would 500 the webhook back to Stripe
    # AFTER the commit succeeded and trigger a redelivery storm / double commit).
    try:
        await MetronomeFastGate().add_credit(owner_id, microcents=amount_microcents)
    except Exception:
        logger.exception(
            "Metronome fast-gate re-arm failed after funding owner %s session %s "
            "(commit landed; next low-balance webhook will re-sync)",
            owner_id,
            stripe_session_id,
        )
