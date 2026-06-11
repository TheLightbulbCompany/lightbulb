"""Idempotent Metronome customer + contract provisioning.

Creates a Metronome customer whose ingest_aliases include our owner_id (so
the meter Lambda's usage events carry customer_id = owner_id, no translation
table), links the EXISTING Stripe customer (root-level billing-provider
configuration) so Metronome pushes usage/credit invoices onto the same
Stripe Customer as the $50/mo subscription, creates the per-customer
contract referencing the env's rate card WITH a contract-level billing
config when that link exists (contracts are per-customer objects in
Metronome — commits and balances hang off them, and payment-gated top-up
commits collect through the contract's billing config, which is only valid
once the customer's root-level config exists — hence the link-then-contract
order), and creates the per-customer low-balance alerts
(warn + zero-crossing) that feed the fast-gate webhook. The Metronome customer UUID + contract id are persisted
on the billing row so a re-call short-circuits all the HTTP calls.

Gated on METRONOME_LIFECYCLE_ENABLED: until the runbook (API tokens + rate
card + webhook secret) is done, the ~3s /container/status poll must not spam
doomed calls against placeholder secrets.

Crash-safety: the ids are persisted LAST, after the alerts exist. A retry
after any partial failure re-enters via create_customer/create_contract,
whose conflict responses are adopted (lookup by ingest alias / contract
list) instead of failing — so a half-provisioned customer converges instead
of stranding.
"""

from __future__ import annotations

import logging

import httpx

from core.billing import metronome_client
from core.billing.metronome_client import MetronomeClient, MetronomeError
from core.config import settings
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# Our balance is PREPAID COMMITS hanging off the per-customer contract, so
# the commit-balance alert type is the one that tracks it. Sandbox-verified:
# "low_remaining_credit_balance_reached" is NOT in the alert-create enum
# (the validator rejects it); "low_remaining_commit_balance_reached" is.
_LOW_BALANCE_ALERT_TYPE = "low_remaining_commit_balance_reached"


class ProvisioningError(Exception):
    pass


async def ensure_metronome_customer(
    *,
    owner_id: str,
    client: MetronomeClient | None = None,
) -> str | None:
    """Return the Metronome customer UUID, creating + contract-arming +
    Stripe-linking + alert-arming on first call. Idempotent on the billing
    row's persisted metronome ids. Returns None (a pure no-op, no HTTP
    client constructed) while the lifecycle switch is off."""
    if not settings.METRONOME_LIFECYCLE_ENABLED:
        return None

    account = await billing_repo.get_by_owner_id(owner_id)
    if account is None:
        raise ProvisioningError(f"No billing row for owner_id={owner_id} — cannot provision Metronome customer")

    existing = account.get("metronome_customer_id")
    if existing and account.get("metronome_contract_id"):
        return existing

    if client is None:
        async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL) as http:
            return await _provision(owner_id, account, metronome_client.for_settings(http))
    return await _provision(owner_id, account, client)


async def _provision(owner_id: str, account: dict, client: MetronomeClient) -> str:
    # Re-use ids a prior partial run already persisted (e.g. a customer
    # provisioned before contracts existed): only the missing pieces are
    # created, and the conflict-adopt paths converge the rest.
    customer_id = account.get("metronome_customer_id") or await _create_or_adopt_customer(owner_id, client)
    # Billing-provider config BEFORE the contract: the contract is created
    # WITH a billing_provider_configuration so payment-gated top-ups can
    # collect, and that object is only valid once the customer's root-level
    # config exists (else contract create 404s "Billing provider
    # configuration not found for STRIPE").
    stripe_linked = await _maybe_link_stripe(owner_id, account, customer_id, client)
    contract_id = account.get("metronome_contract_id") or await _create_or_adopt_contract(
        customer_id, client, with_stripe_billing=stripe_linked
    )
    await _ensure_low_balance_alerts(owner_id, customer_id, client)
    # Persist LAST: the short-circuit keys on these attributes, so persisting
    # before the alerts exist would silently leave the owner alert-less
    # (no webhook -> the fast gate never learns about a depleted balance).
    await billing_repo.set_metronome_ids(
        owner_id,
        metronome_customer_id=customer_id,
        metronome_contract_id=contract_id,
    )
    return customer_id


async def _maybe_link_stripe(owner_id: str, account: dict, customer_id: str, client: MetronomeClient) -> bool:
    """Set the customer's root-level billing-provider configuration (Stripe
    customer + charge_automatically) when the billing row carries a
    stripe_customer_id. Returns whether the config now exists — the caller
    only attaches a contract-level billing config when it does (a contract
    referencing a non-existent customer config 404s at create).

    Payment-gated top-up commits collect through this link, so it matters —
    but it is BEST-EFFORT here: a failure (or a row with no Stripe customer
    yet) must not block provisioning the Metronome customer/contract/alerts.
    The contract is then created WITHOUT billing config, so top-ups for that
    owner fail loudly at the payment gate (never silently) until the owner
    is re-provisioned; the warnings + metrics below make the gap
    operator-visible.
    """
    stripe_customer_id = account.get("stripe_customer_id")
    if not stripe_customer_id:
        from core.observability.metrics import put_metric

        put_metric("metronome.provision.no_stripe_customer")
        logger.warning(
            "Billing row for owner %s has no stripe_customer_id — Metronome contract will carry no "
            "billing config; payment-gated top-ups will fail until re-provisioned",
            owner_id,
        )
        return False
    try:
        await client.set_stripe_billing_config(
            metronome_customer_id=customer_id,
            stripe_customer_id=stripe_customer_id,
        )
        return True
    except Exception:
        from core.observability.metrics import put_metric

        put_metric("metronome.provision.stripe_link_error")
        logger.exception(
            "Failed to set Stripe billing-provider config for owner %s (Metronome customer %s) — "
            "contract will carry no billing config; payment-gated top-ups will fail until re-provisioned",
            owner_id,
            customer_id,
        )
        return False


async def _create_or_adopt_customer(owner_id: str, client: MetronomeClient) -> str:
    try:
        return await client.create_customer(name=owner_id, ingest_aliases=[owner_id])
    except httpx.HTTPStatusError as exc:
        # Alias conflict: a prior attempt created the customer but crashed
        # before the UUID was persisted (or the billing row was rebuilt).
        # The alias is the durable identity, so adopt the existing customer
        # instead of failing the retry forever.
        if exc.response.status_code != 409:
            raise
        adopted = await client.get_customer_id_by_ingest_alias(owner_id)
        if not adopted:
            raise MetronomeError(
                f"create_customer conflicted on ingest alias {owner_id!r} but no customer matches it"
            ) from exc
        logger.info("Adopted existing Metronome customer %s for owner %s", adopted, owner_id)
        return adopted


async def _create_or_adopt_contract(customer_id: str, client: MetronomeClient, *, with_stripe_billing: bool) -> str:
    try:
        # rate_card_id is optional on contract create (sandbox-verified) and
        # is forwarded only when the env actually has one configured.
        return await client.create_contract(
            customer_id=customer_id,
            rate_card_id=settings.METRONOME_RATE_CARD_ID or None,
            with_stripe_billing=with_stripe_billing,
        )
    except httpx.HTTPStatusError as exc:
        # Conflict: a prior attempt created the contract but crashed before
        # its id was persisted. One customer carries at most the one contract
        # this module creates, so adopting the first listed contract is safe.
        if exc.response.status_code != 409:
            raise
        contracts = await client.list_contracts(customer_id=customer_id)
        adopted = contracts[0].get("id") if contracts else None
        if not adopted:
            raise MetronomeError(
                f"create_contract conflicted for customer {customer_id} but no contract is listed"
            ) from exc
        logger.info("Adopted existing Metronome contract %s for customer %s", adopted, customer_id)
        return adopted


async def _ensure_low_balance_alerts(owner_id: str, customer_id: str, client: MetronomeClient) -> None:
    """Arm BOTH the warning alert and the zero-crossing alert.

    Metronome alerts fire once on crossing into in_alarm, with
    remaining_balance ≈ the alert's threshold — so the warn alert always
    delivers a POSITIVE remaining and the ≤0 kill in enforce_on_low_balance
    would never fire off it. The -zero alert (threshold 0) is the one that
    actually delivers remaining ≤ 0; the webhook handler itself needs no
    special-casing.
    """
    alerts = (
        ("warn", settings.METRONOME_LOW_BALANCE_THRESHOLD_CENTS),
        ("zero", 0),
    )
    for suffix, threshold_cents in alerts:
        try:
            await client.create_alert(
                customer_id=customer_id,
                name=f"isol8-low-balance-{owner_id}-{suffix}",
                alert_type=_LOW_BALANCE_ALERT_TYPE,
                threshold_cents=threshold_cents,
                # Required for commit-balance alert types (sandbox-verified:
                # the create validator rejects the body without it).
                credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
            )
        except httpx.HTTPStatusError as exc:
            # A duplicate alert (re-entry after a crash between alert-create
            # and the ids persist) is success — the alert we want already
            # exists.
            if exc.response.status_code != 409:
                raise
            logger.info(
                "Low-balance alert %s already exists for Metronome customer %s",
                suffix,
                customer_id,
            )
