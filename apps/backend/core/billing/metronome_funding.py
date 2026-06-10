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

import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.billing.metronome_units import microcents_to_cents
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)


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
    provider_choice: str | None = None,
) -> None:
    """Add an ungated prepaid commit for amount_microcents.

    The Metronome customer UUID + contract id + provider_choice live on the
    billing row (where ensure_metronome_customer persists the ids at
    provision time). Callers that already hold the row (e.g. the Stripe
    webhook, which reads it to decide whether the owner is Metronome-
    provisioned at all) pass all three through to skip the duplicate
    billing-row read.
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
        provider_choice = provider_choice or account.get("provider_choice")
    if not metronome_customer_id:
        raise FundingError(f"Metronome customer not provisioned for owner_id={owner_id}")
    if not metronome_contract_id:
        raise FundingError(f"Metronome contract not provisioned for owner_id={owner_id}")

    # The commit body itself (flat customerCommits/create shape, no
    # payment_gate_config) is owned by the client layer; this module only
    # supplies the money in CENTS (Metronome's USD credit type — see
    # core/billing/metronome_units.py) and the audit description.
    amount_cents = microcents_to_cents(amount_microcents)
    description = f"isol8 top-up stripe_session={stripe_session_id} owner={owner_id}"

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            await metronome_client.for_settings(http).create_prepaid_commit(
                customer_id=metronome_customer_id,
                contract_id=metronome_contract_id,
                product_id=product_id,
                credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
                amount_cents=amount_cents,
                description=description,
            )
    else:
        await client.create_prepaid_commit(
            customer_id=metronome_customer_id,
            contract_id=metronome_contract_id,
            product_id=product_id,
            credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
            amount_cents=amount_cents,
            description=description,
        )

    # Un-kill: re-attach the per-owner scoped Bedrock policy now that the
    # owner is funded again — the kill-switch may have revoked it when the
    # balance crossed zero, and Metronome fires no recovery webhook to undo
    # it. Only Bedrock-managed owners have a policy to restore: BYO/OAuth
    # owners carry no AIP map, so calling restore for them would just log
    # noise on every top-up. Idempotent when the kill never fired.
    # Best-effort for the same reason as the re-arm below: the commit
    # already landed against a confirmed payment, so a restore failure must
    # not 500 the webhook back to Stripe (redelivery storm / double commit).
    # Imported lazily to keep the IAM/container layer out of this module's
    # import tree.
    if provider_choice == "bedrock_claude":
        try:
            from core.billing.bedrock_aip import restore_owner_bedrock

            await restore_owner_bedrock(owner_id)
        except Exception as exc:
            if "no persisted AIP map" in str(exc):
                # Expected state (container never provisioned / wiped), not
                # an IAM failure worth an error-level stack trace.
                logger.info(
                    "Bedrock restore skipped after funding owner %s session %s: %s",
                    owner_id,
                    stripe_session_id,
                    exc,
                )
            else:
                logger.exception(
                    "Bedrock restore failed after funding owner %s session %s "
                    "(commit landed; gate re-arm still proceeds)",
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
