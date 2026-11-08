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

Crash-safety: the ids are persisted LAST, after the alerts exist. A retry
after any partial failure re-enters via create_customer/create_contract,
whose conflict responses are adopted (lookup by ingest alias / contract
list) instead of failing — so a half-provisioned customer converges instead
of stranding.

Who retries a failure: every caller (the durable reconcile execution, the Stripe
``entering_live`` webhook, the on-demand credit-top-up path) owns its own retry —
not this module. ``hermes_provisioning.reconcile_runtime`` fails its execution
on an error here, so the state machine retries it.
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


# Subscription statuses that get a Metronome ledger. THE definition of "who
# gets a ledger" — `ensure_metronome_customer` enforces it and the backfill
# script selects on it, so the live path and the one-shot can never disagree
# about which owners are supposed to have one.
#
# Deliberately its own axis, not a reuse of provision_gate's predicates:
#   is_subscription_active  {active, trialing, trial_local}  "may use the platform now"
#   is_paid_subscription    {active, trialing}               "renders as subscribed"
#   here                    {active, trialing, past_due, unpaid}
#
# trial_local is excluded because free-tier owners draw no prepaid credits —
# a ledger for them is pollution AND (worse) gives them a zero balance the
# low-balance enforcer reads as "out of credits" and revokes Bedrock on.
# past_due/unpaid ARE included: those subscriptions are live and recoverable,
# a ledger they cannot yet spend from blocks nothing, and having it ready
# means recovery does not also need a provisioning round trip.
# canceled/incomplete/incomplete_expired/paused are excluded — they cannot
# convert without a fresh checkout, which re-enters this path anyway.
LEDGER_ELIGIBLE_STATUSES = frozenset({"active", "trialing", "past_due", "unpaid"})


def is_ledger_eligible(account: dict | None) -> bool:
    """Whether this owner should have a Metronome ledger at all.

    The legacy fallback mirrors ``provision_gate.is_subscription_active``: a
    row with ``subscription_status is None`` and a ``stripe_subscription_id``
    is a real paid subscription whose status was never backfilled by
    ``customer.subscription.updated``, and the gates treat it as active — so
    it must get a ledger too, or the fences would lock out exactly the owners
    they consider paid. Strict None guard, matching that function: an explicit
    non-eligible status can never ride a stale subscription id into eligibility.
    """
    if not account:
        return False
    from core.billing.voice_billing import has_voice_plan

    if has_voice_plan(account) and account.get("provider_choice") == "openrouter_managed":
        return True  # explicit premium managed inference still needs its separate owner-funded credit ledger
    status = account.get("subscription_status")
    if status in LEDGER_ELIGIBLE_STATUSES:
        return True
    return status is None and bool(account.get("stripe_subscription_id"))


async def ensure_metronome_customer(
    *,
    owner_id: str,
    client: MetronomeClient | None = None,
) -> str | None:
    """Return the Metronome customer UUID, creating + contract-arming +
    Stripe-linking + alert-arming on first call. Idempotent on the billing
    row's persisted metronome ids."""
    # STRONGLY consistent: the subscription webhook dispatches this immediately
    # after ``set_subscription`` flips the owner off ``trial_local``, and an
    # eventually-consistent get_item can still observe the pre-write row. That
    # would hit the free-tier short-circuit below and return None with no
    # customer, no exception and no metric — and the webhook never re-enters
    # that branch on redelivery (old_status is already the new status), so the
    # converted owner would be left without a ledger permanently. This is a
    # read-back-after-write caller; the repo supports exactly that.
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if account is None:
        raise ProvisioningError(f"No billing row for owner_id={owner_id} — cannot provision Metronome customer")

    # Only eligible owners get a ledger — see LEDGER_ELIGIBLE_STATUSES for why
    # each status is in or out. This used to test ONLY `== "trial_local"`,
    # which was sufficient while the sole caller sat BEHIND the provision gate:
    # a canceled or paused owner never reached it, because the gate returned
    # first. Callers run this BEFORE the gate (the gate depends on the
    # ledger existing, so it cannot also guard it), which removed that
    # filter — and a canceled owner reaching such a caller would have had a real customer + contract + two low-balance alerts
    # created over live HTTP, for an account that can never spend from them.
    # The guard belongs here rather than at the caller: this function owns
    # "who gets a ledger", and every caller routes through it.
    #
    # On conversion to a paid plan, subscription_status flips and this runs
    # normally.
    if not is_ledger_eligible(account):
        return None

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
    if not stripe_linked and not account.get("metronome_contract_id"):
        # A contract's billing config is create-time-only and the
        # short-circuit keys on both persisted ids — creating the contract
        # now would permanently strand the owner with payment-gated top-ups
        # that can never collect. Persist just the customer UUID and stop:
        # the next /container/status poll re-enters and retries the link.
        if account.get("metronome_customer_id") != customer_id:
            await billing_repo.set_metronome_customer_id(owner_id, metronome_customer_id=customer_id)
        return customer_id
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
    creates the contract only when it does (a contract referencing a
    non-existent customer config 404s at create, and a contract created
    WITHOUT a billing config can never collect payment-gated top-ups —
    billing config is create-time-only).

    Payment-gated top-up commits collect through this link, so it matters —
    but it is BEST-EFFORT here: a failure (or a row with no Stripe customer
    yet) must not raise and block the customer create from persisting. The
    caller instead DEFERS contract creation until a later poll re-enters
    with a working link; the warnings + metrics below make a persistent gap
    operator-visible.
    """
    stripe_customer_id = account.get("stripe_customer_id")
    if not stripe_customer_id:
        from core.observability.metrics import put_metric

        put_metric("metronome.provision.no_stripe_customer")
        logger.warning(
            "Billing row for owner %s has no stripe_customer_id — deferring Metronome contract "
            "creation until the Stripe customer exists",
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
            "deferring contract creation; the next status poll retries the link",
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
    # rate_card_id is optional on contract create (sandbox-verified) and is
    # forwarded only when the env actually has one configured.
    rate_card_id = settings.METRONOME_RATE_CARD_ID or None
    # Adopt a live contract on this rate card before creating one: overlapping
    # contracts on one rate card each price the same usage, and a customer can
    # already carry an UNKEYED contract from before the key existed (or one
    # whose id never reached the billing row). Oldest first, as listed.
    existing = next(
        (
            c.get("id")
            for c in await client.list_contracts(customer_id=customer_id)
            if c.get("rate_card_id") == rate_card_id
        ),
        None,
    )
    if existing:
        logger.info("Adopted existing Metronome contract %s for customer %s", existing, customer_id)
        return existing
    # Deterministic per (customer, rate card) so the race between concurrent
    # provisions (status polls across backend tasks, the entering_live webhook)
    # ends in a 409 for the loser. Keyed on the customer UUID, not the owner id,
    # so a wiped owner's fresh customer is not blocked by its archived one.
    # Contract archive keeps the key: bump the version after archiving the keyed contract.
    uniqueness_key = f"lightbulb-contract:{customer_id}:{rate_card_id or 'none'}:v1"
    try:
        return await client.create_contract(
            customer_id=customer_id,
            rate_card_id=rate_card_id,
            with_stripe_billing=with_stripe_billing,
            uniqueness_key=uniqueness_key,
        )
    except httpx.HTTPStatusError as exc:
        # Conflict on the key: a concurrent attempt created this contract
        # between our lookup and our create — adopt it instead of failing.
        if exc.response.status_code != 409:
            raise
        contracts = await client.list_contracts(customer_id=customer_id)
        adopted = next((c.get("id") for c in contracts if c.get("uniqueness_key") == uniqueness_key), None)
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
    delivers a POSITIVE remaining and would never block off it. The -zero
    alert (threshold 0) is the one that actually delivers remaining ≤ 0; the webhook handler itself needs no
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
                name=f"lightbulb-low-balance-{owner_id}-{suffix}",
                alert_type=_LOW_BALANCE_ALERT_TYPE,
                threshold_cents=threshold_cents,
                # Required for commit-balance alert types (sandbox-verified:
                # the create validator rejects the body without it).
                credit_type_id=settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID,
                # Makes the 409 branch below real: concurrent provisions
                # otherwise arm duplicate alerts. Alert archive can release
                # the key (release_uniqueness_key), so no version segment.
                uniqueness_key=f"lightbulb-low-balance:{customer_id}:{suffix}",
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
