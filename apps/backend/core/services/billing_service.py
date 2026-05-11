"""Service for Stripe billing operations — flat-fee model."""

import asyncio
import hashlib
import logging
import os
import time
from typing import Literal

import stripe

from core.config import settings
from core.observability.metrics import put_metric, timing
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

ProviderChoice = Literal["chatgpt_oauth", "byo_key", "bedrock_claude"]
ByoProvider = Literal["openai", "anthropic"]


class ChangeProviderError(Exception):
    """Raised when a post-onboarding provider switch is rejected.

    ``code`` is a short machine-readable string the router maps to an
    HTTP status; ``message`` is the user-facing detail.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


stripe.api_key = settings.STRIPE_SECRET_KEY

FRONTEND_URL = os.getenv(
    "FRONTEND_URL", settings.cors_origins_list[0] if settings.cors_origins_list else "http://localhost:3000"
)


class BillingServiceError(Exception):
    pass


class BillingService:
    async def create_customer_for_owner(
        self, owner_id: str, owner_type: str = "personal", email: str | None = None
    ) -> dict:
        """Resolve a Stripe customer for ``owner_id``, keyed by ``email``.

        Email is the system-of-record idempotency key for Stripe-side identity:
        a single human (one verified email in Clerk) maps to a single Stripe
        Customer, regardless of how many billing entities they own (personal
        account, multiple orgs as admin, etc.). DDB ``billing-accounts`` rows
        are still per-owner_id, so multiple rows can share one
        ``stripe_customer_id``.

        Resolution order:
          1. Existing DDB row for this owner_id → return it (no Stripe call).
          2. ``stripe.Customer.list(email=...)`` → reuse the most recent match.
             ``list`` is exact-match and immediately consistent (unlike
             ``search``, which has indexing lag).
          3. No match → ``stripe.Customer.create``.

        Webhook handlers must resolve owner via subscription metadata
        (``owner_id``) since the GSI on ``stripe_customer_id`` is no longer
        unique. See ``handle_stripe_webhook`` in ``routers/billing.py``.
        """
        existing = await billing_repo.get_by_owner_id(owner_id)
        if existing:
            return existing

        # Normalize so a single human typing/signing in with mixed case
        # ("User@Example.com" vs "user@example.com") still maps to one
        # Stripe customer. Stripe's Customer.list email filter is exact
        # match, so without normalization we'd silently create duplicates.
        normalized_email = email.strip().lower() if email else None

        customer_id: str | None = None
        created_locally = False
        if normalized_email:
            with timing("stripe.api.latency", {"op": "customers.list"}):
                matches = stripe.Customer.list(email=normalized_email, limit=1)
            if matches and matches.data:
                customer_id = matches.data[0].id
                put_metric("stripe.customer.email_dedup", dimensions={"result": "reuse"})
                logger.info(
                    "Reusing Stripe customer %s for owner_id=%s (email=%s)",
                    customer_id,
                    owner_id,
                    normalized_email,
                )

        if customer_id is None:
            with timing("stripe.api.latency", {"op": "customers.create"}):
                customer = stripe.Customer.create(
                    email=normalized_email,
                    metadata={"owner_id": owner_id, "owner_type": owner_type},
                )
            customer_id = customer.id
            created_locally = True
            put_metric("stripe.customer.email_dedup", dimensions={"result": "create"})

        try:
            return await billing_repo.create_if_not_exists(
                owner_id=owner_id,
                stripe_customer_id=customer_id,
                owner_type=owner_type,
            )
        except billing_repo.AlreadyExistsError:
            # Lost the DDB race for this owner_id. If we took the create
            # path, two concurrent callers may have both missed the
            # email-list lookup and each minted a fresh Stripe customer —
            # the loser's customer is now orphaned (no DDB row points at
            # it). Delete it so the email→customer invariant holds.
            winner = await billing_repo.get_by_owner_id(owner_id)
            if (
                created_locally
                and winner
                and winner.get("stripe_customer_id")
                and winner["stripe_customer_id"] != customer_id
            ):
                logger.info(
                    "Lost DDB race for owner_id=%s; deleting orphan Stripe customer %s (winner=%s)",
                    owner_id,
                    customer_id,
                    winner["stripe_customer_id"],
                )
                try:
                    with timing("stripe.api.latency", {"op": "customers.delete"}):
                        stripe.Customer.delete(
                            customer_id,
                            idempotency_key=f"delete_customer:{customer_id}",
                        )
                    put_metric("stripe.customer.orphan_delete", dimensions={"result": "ok"})
                except Exception:
                    put_metric("stripe.customer.orphan_delete", dimensions={"result": "error"})
                    logger.warning("Failed to delete orphan Stripe customer %s", customer_id)
            return winner

    async def create_portal_session(self, billing_account: dict) -> str:
        owner_id = billing_account["owner_id"]
        # 5-minute time bucket — same retry-collapsing intent as checkout.
        bucket = int(time.time() // 300)
        with timing("stripe.api.latency", {"op": "billing_portal.session.create"}):
            session = stripe.billing_portal.Session.create(
                customer=billing_account["stripe_customer_id"],
                return_url=f"{FRONTEND_URL}/settings/billing",
                idempotency_key=f"portal:{owner_id}:{bucket}",
            )
        return session.url

    async def cancel_subscription(self, billing_account: dict) -> None:
        await billing_repo.set_subscription(
            owner_id=billing_account["owner_id"],
            subscription_id=None,
            status="canceled",
            trial_end=None,
        )


async def create_flat_fee_checkout(
    *,
    owner_id: str,
    provider_choice: str | None = None,
    byo_provider: str | None = None,
    clerk_user_id: str | None = None,
    trial_days: int | None = 14,
) -> stripe.checkout.Session:
    """Create a Stripe Checkout session on the single flat-fee price.

    Used by the flat-fee onboarding wizard (frontend cards 1, 2, 3 — all three
    pay the same monthly fee against ``STRIPE_FLAT_PRICE_ID``). When
    ``trial_days`` is set, the resulting subscription has a trial of that
    length and Stripe charges nothing until the trial converts.

    ``provider_choice``, ``clerk_user_id`` and ``owner_id`` are threaded into
    ``subscription_data.metadata`` so the ``customer.subscription.*`` webhook
    can resolve the owner unambiguously. ``owner_id`` matters because the
    Stripe customer is shared across personal/org billing rows for the same
    email — the GSI on ``stripe_customer_id`` is no longer 1:1.

    Conventions (mirrors Plan 1 Stripe Tax setup):
      - ``automatic_tax={"enabled": True}`` — Stripe computes sales tax/VAT.
      - ``customer_update={"address": "auto"}`` — required when automatic_tax
        is enabled so Checkout can persist the collected billing address back
        onto the existing Stripe customer.
      - ``allow_promotion_codes=True`` — surfaces the promo-code field so
        internal coupons (e.g. 100%-off launch codes) can be applied.
      - ``idempotency_key`` bucketed to a 5-minute window: collapses
        rapid duplicate clicks (and "open Stripe Checkout in two tabs"
        style retries) to a single Checkout Session, so a user can't
        accidentally complete two parallel subscriptions before the
        first ``customer.subscription.created`` webhook lands and the
        guard in ``create_trial_checkout`` kicks in. Stripe's
        idempotency does a body-shape compare, so a config change
        (e.g. ``allow_promotion_codes`` flipping on) naturally
        bypasses the cached response within the same bucket.
    """
    if not settings.STRIPE_FLAT_PRICE_ID:
        raise BillingServiceError("STRIPE_FLAT_PRICE_ID not configured")

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    subscription_data: dict = {}
    if trial_days is not None and trial_days > 0:
        subscription_data["trial_period_days"] = trial_days
    metadata: dict = {"owner_id": owner_id}
    if provider_choice:
        metadata["provider_choice"] = provider_choice
    if byo_provider:
        metadata["byo_provider"] = byo_provider
    if clerk_user_id:
        metadata["clerk_user_id"] = clerk_user_id
    subscription_data["metadata"] = metadata

    kwargs: dict = dict(
        customer=account["stripe_customer_id"],
        mode="subscription",
        line_items=[{"price": settings.STRIPE_FLAT_PRICE_ID, "quantity": 1}],
        success_url=(
            f"{FRONTEND_URL}/chat?checkout=success" + (f"&provider={provider_choice}" if provider_choice else "")
        ),
        cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
        allow_promotion_codes=True,
        automatic_tax={"enabled": True},
        customer_update={"address": "auto"},
        subscription_data=subscription_data,
        idempotency_key=f"flat_checkout:{owner_id}:{provider_choice or '_'}:{int(time.time() // 300)}",
    )

    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(**kwargs)
    return session


async def create_credit_top_up_checkout(
    *,
    owner_id: str,
    amount_cents: int,
) -> stripe.checkout.Session:
    """Create a Stripe Checkout session for a one-shot Claude-credit top-up.

    Replaces the legacy inline-Elements flow (PaymentIntent + Stripe.js)
    with a server-rendered Checkout page. Wins:

      - ``allow_promotion_codes=True`` lets internal users apply your
        existing 100%-off coupon directly on the Checkout page — Stripe
        handles validation and discount math, and the resulting
        ``$0`` charge incurs no Stripe fee.
      - No publishable key needed in the frontend bundle (the
        ``NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY`` env-var bug class goes
        away entirely).
      - Apple Pay / Google Pay / Link / saved cards / 3DS / SCA all
        come for free.

    Conventions match :func:`create_flat_fee_checkout`:
      - ``automatic_tax`` enabled + ``customer_update.address=auto``.
      - ``idempotency_key`` includes a per-request nonce because legitimate
        repeat top-ups for the same amount must produce distinct sessions
        — bucketing on owner_id+amount would silently collapse a second
        top-up onto the first.

    The credit grant happens asynchronously when Stripe fires
    ``checkout.session.completed`` with metadata
    ``{"purpose": "credit_top_up", "owner_id": ...}``. ``owner_id`` is the
    org_id in org context, the user_id in personal context — i.e. whatever
    ``resolve_owner_id`` returns for the requesting auth context. Pooling
    at the owner level lets every member of an org draw from one balance.
    """
    if amount_cents < 500:
        raise BillingServiceError("Minimum top-up is $5 (500 cents)")

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    # Per-request nonce — see docstring. Hashed to keep the key short.
    nonce = hashlib.sha1(os.urandom(16)).hexdigest()[:12]

    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(
            customer=account["stripe_customer_id"],
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": "Claude credits",
                            "description": ("Prepaid Claude inference balance. Credits deducted at 1.4× Bedrock cost."),
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=(f"{FRONTEND_URL}/chat?credits=success"),
            cancel_url=f"{FRONTEND_URL}/chat?credits=cancel",
            allow_promotion_codes=True,
            automatic_tax={"enabled": True},
            customer_update={"address": "auto"},
            payment_intent_data={
                "metadata": {
                    "purpose": "credit_top_up",
                    "owner_id": owner_id,
                },
            },
            metadata={
                "purpose": "credit_top_up",
                "owner_id": owner_id,
                "amount_cents": str(amount_cents),
            },
            idempotency_key=f"credit_checkout:{owner_id}:{nonce}",
        )
    return session


async def create_trial_subscription(*, owner_id: str, payment_method_id: str) -> stripe.Subscription:
    """Create a Stripe Subscription with a 14-day trial.

    Per spec §7.1 / §7.3: backend creates the Subscription IMMEDIATELY at
    signup against the saved payment method. Stripe handles conversion on
    day 15 (Smart Retries on failure). Backend just listens to the
    resulting webhooks (Plan 3 Task 2).

    The subscription is born in ``status: trialing`` with ``trial_end`` 14
    days out. Subscription id + initial status are persisted via
    :func:`billing_repo.set_subscription` so the rest of the system can
    read trial state without re-querying Stripe.

    Conventions:
      - ``automatic_tax={"enabled": True}`` — same Stripe Tax setup as
        :func:`create_flat_fee_checkout`.
      - ``payment_behavior="default_incomplete"`` — surfaces 3DS challenges
        to the frontend instead of auto-failing the create call.
      - ``payment_settings.save_default_payment_method=on_subscription``
        means a successful first charge persists the PM as the default
        for future invoices (no re-prompt at trial conversion).
      - ``idempotency_key=f"trial_signup:{owner_id}"`` — deterministic per
        user. A retry of the same trial-create call returns the same
        Stripe subscription, never duplicates.
    """
    if not settings.STRIPE_FLAT_PRICE_ID:
        raise BillingServiceError("STRIPE_FLAT_PRICE_ID not configured")

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    with timing("stripe.api.latency", {"op": "subscription.create"}):
        sub = stripe.Subscription.create(
            customer=account["stripe_customer_id"],
            items=[{"price": settings.STRIPE_FLAT_PRICE_ID}],
            trial_period_days=14,
            default_payment_method=payment_method_id,
            automatic_tax={"enabled": True},
            payment_behavior="default_incomplete",
            payment_settings={
                "save_default_payment_method": "on_subscription",
                "payment_method_types": ["card"],
            },
            idempotency_key=f"trial_signup:{owner_id}",
        )

    await billing_repo.set_subscription(
        owner_id=owner_id,
        subscription_id=sub.id,
        status=sub.status,
        trial_end=getattr(sub, "trial_end", None),
    )
    return sub


# ---------------------------------------------------------------------------
# Module-level admin wrappers
# ---------------------------------------------------------------------------
#
# Used by routers/admin.py — operate by Clerk user_id rather than billing_account
# dict so the admin router doesn't have to load the row first. Each function
# loads billing_repo + calls Stripe + updates DDB. Errors raise BillingServiceError.


async def cancel_subscription_for_owner(user_id: str) -> dict:
    """Admin: cancel a user's Stripe subscription. Idempotent on no-sub."""
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing:
        return {"status": "no_billing_account"}
    sub_id = billing.get("stripe_subscription_id")
    if not sub_id:
        return {"status": "no_subscription"}
    try:
        stripe.Subscription.delete(
            sub_id,
            idempotency_key=f"sub_cancel:{sub_id}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_cancel_failed: {e}")
    await BillingService().cancel_subscription(billing)
    return {"status": "cancelled", "subscription_id": sub_id}


async def pause_subscription_for_owner(user_id: str) -> dict:
    """Admin: pause Stripe subscription billing (mark_uncollectible)."""
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_subscription_id"):
        return {"status": "no_subscription"}
    sub_id = billing["stripe_subscription_id"]
    try:
        stripe.Subscription.modify(
            sub_id,
            pause_collection={"behavior": "mark_uncollectible"},
            idempotency_key=f"sub_modify:{sub_id}:pause",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_pause_failed: {e}")
    return {"status": "paused", "subscription_id": sub_id}


async def issue_credit_for_owner(user_id: str, *, amount_cents: int, reason: str) -> dict:
    """Admin: credit the user's Stripe customer balance by amount_cents.

    Stripe applies negative balance amounts as a credit toward future invoices.
    """
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_customer_id"):
        return {"status": "no_customer"}
    customer_id = billing["stripe_customer_id"]
    # No admin_action_id is threaded through here yet, so derive a stable
    # hash from (amount, reason). Two genuinely-different credits with the
    # same reason but different amounts get different keys; a retry of the
    # same logical credit collapses to one Stripe write.
    reason_hash = hashlib.sha256(f"{amount_cents}:{reason}".encode("utf-8")).hexdigest()[:16]
    try:
        txn = stripe.Customer.create_balance_transaction(
            customer_id,
            amount=-abs(amount_cents),
            currency="usd",
            description=reason,
            idempotency_key=f"balance_tx:{customer_id}:{reason_hash}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_credit_failed: {e}")
    return {"status": "credited", "transaction_id": txn["id"], "amount_cents": amount_cents}


async def mark_invoice_resolved(user_id: str, invoice_id: str) -> dict:
    """Admin: mark a Stripe invoice as paid out-of-band (e.g. wire transfer)."""
    try:
        invoice = stripe.Invoice.pay(
            invoice_id,
            paid_out_of_band=True,
            idempotency_key=f"invoice_pay:{invoice_id}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_invoice_pay_failed: {e}")
    return {"status": "resolved", "invoice_id": invoice_id, "stripe_status": invoice.get("status")}


# ---------------------------------------------------------------------------
# Post-onboarding provider switch (POST /billing/provider)
# ---------------------------------------------------------------------------
#
# Mid-flight provider changes have to do three things atomically:
#   1. Flip provider_choice (+ byo_provider) on the billing row.
#   2. Clear the *old* provider's side-effect state (OAuth tokens, BYOK
#      Secrets Manager secrets) so a future re-switch doesn't read stale
#      credentials. Cleared AFTER the new state is committed — partial
#      failure leaves the owner with an extra row to garbage-collect, never
#      an owner stranded with no provider.
#   3. Re-provision the container. The new provider needs different
#      task-def secrets (BYOK env vars) or different EFS pre-staging
#      (chatgpt_oauth auth-profiles.json), neither of which a Track-1
#      openclaw.json patch alone can achieve. Re-provisioning ends the
#      current chat session — the frontend surfaces this as an explicit
#      cutover warning before calling the endpoint.


async def change_provider(
    *,
    owner_id: str,
    clerk_user_id: str,
    owner_type: str,
    is_org_context: bool,
    provider_choice: ProviderChoice,
    byo_provider: ByoProvider | None = None,
) -> dict:
    """Apply a post-onboarding provider switch for an existing subscriber.

    Args:
        owner_id: org_id for org context, user_id for personal. Pins the
            billing row + the container scope (re-provision target).
        clerk_user_id: The signed-in human's Clerk user_id. Used for
            personal-only side-effect reads (OAuth tokens, BYOK keys live
            keyed on the calling user, not the org).
        owner_type: ``"personal"`` or ``"org"``. Forwarded to set_provider_choice
            so the org-invariant check (chatgpt_oauth banned on org rows)
            fires at the repo layer too.
        is_org_context: Whether the caller is in org mode. Used to enforce
            the chatgpt_oauth=personal-only guard before any state changes.
        provider_choice: Target provider.
        byo_provider: Required when ``provider_choice == "byo_key"``; one of
            "openai" or "anthropic".

    Returns:
        ``{"status": "ok"|"noop", "provider_choice": ..., "byo_provider": ...}``.
        ``noop`` means the requested state matched the current billing-row
        state and no work was done.

    Raises:
        ChangeProviderError: with a code + status_code the router maps to HTTP.
    """
    # Org guard — chatgpt_oauth is personal-only
    # (memory/project_chatgpt_oauth_personal_only.md). Mirror the same guard
    # /billing/trial-checkout uses so a savvy caller can't bypass it by
    # going through the post-onboarding switch endpoint.
    if provider_choice == "chatgpt_oauth" and is_org_context:
        raise ChangeProviderError(
            "org_chatgpt_oauth_forbidden",
            "ChatGPT OAuth is not available for organization workspaces.",
            status_code=403,
        )

    # Subscription must exist and be currently usable. The post-onboarding
    # switch endpoint is NOT a substitute for /billing/trial-checkout —
    # if there's no subscription we route the caller back through the
    # signup picker rather than mutating provider state on a dead row.
    from core.services.provision_gate import is_subscription_active

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account:
        raise ChangeProviderError(
            "no_billing_account",
            "No billing account. Subscribe via /onboarding first.",
            status_code=404,
        )
    if not is_subscription_active(account):
        raise ChangeProviderError(
            "subscription_inactive",
            "Subscription is not active. Manage it in /settings/billing.",
            status_code=409,
        )

    current_choice = account.get("provider_choice")
    current_byo = account.get("byo_provider")
    next_byo = byo_provider if provider_choice == "byo_key" else None

    # Fast-path no-op. Returning early instead of re-provisioning prevents
    # an idle "change provider" click from blowing away the user's chat
    # session for no functional change. byo_key with the same byo_provider
    # is treated as a no-op too — different byo_provider DOES re-provision
    # because the env-var secret reference changes.
    if current_choice == provider_choice and current_byo == next_byo:
        return {
            "status": "noop",
            "provider_choice": provider_choice,
            "byo_provider": next_byo,
        }

    # Prerequisite check for the *new* provider. Done before the DDB write
    # so a missing prerequisite returns 409 with the chosen state still
    # consistent — the caller can complete the prerequisite step in the UI
    # (OAuth device flow / paste API key) and retry without our row in an
    # intermediate state.
    if provider_choice == "chatgpt_oauth":
        from core.services import oauth_service

        tokens = await oauth_service.get_decrypted_tokens(user_id=clerk_user_id)
        if not tokens:
            raise ChangeProviderError(
                "oauth_required",
                "Complete the ChatGPT sign-in step before switching to ChatGPT OAuth.",
                status_code=409,
            )
    elif provider_choice == "byo_key":
        if byo_provider not in ("openai", "anthropic"):
            raise ChangeProviderError(
                "byo_provider_required",
                "byo_provider must be 'openai' or 'anthropic' when switching to BYO key.",
                status_code=422,
            )
        from core.repositories import api_key_repo

        key_row = await api_key_repo.get_key(clerk_user_id, byo_provider)
        if not key_row or not key_row.get("secret_arn"):
            raise ChangeProviderError(
                "byo_key_missing",
                f"Save your {byo_provider.title()} API key before switching to BYO key.",
                status_code=409,
            )
    # bedrock_claude: no prerequisite. The credits gate fires later via
    # provision_gate.evaluate_provision_gate — we don't pre-block here so
    # owners with zero balance can still pick Bedrock + see the structured
    # gate UI prompting them to top up.

    # Commit the new provider state. Done BEFORE side-effect cleanup so a
    # crash mid-cleanup leaves the owner in a coherent state (new provider
    # + a bit of stale OAuth/BYOK data, which the next sweep can reclaim).
    await billing_repo.set_provider_choice(
        owner_id,
        provider_choice=provider_choice,
        byo_provider=next_byo,
        owner_type=owner_type,
    )

    # Side-effect cleanup: drop the OLD provider's credentials so a future
    # switch back doesn't reuse stale state. Each branch is best-effort —
    # a failure here is logged but does not roll back the billing-row
    # update. The provider switch is the user-visible commit point; orphan
    # secrets/tokens are an operator-sweep concern.
    if current_choice == "chatgpt_oauth" and provider_choice != "chatgpt_oauth":
        from core.containers.workspace import delete_auth_profile_store
        from core.services.oauth_service import revoke_user_oauth

        try:
            await revoke_user_oauth(user_id=clerk_user_id)
        except Exception:
            logger.exception(
                "change_provider: revoke_user_oauth failed for clerk_user_id=%s",
                clerk_user_id,
            )
        try:
            await delete_auth_profile_store(user_id=clerk_user_id)
        except Exception:
            logger.exception(
                "change_provider: delete_auth_profile_store failed for clerk_user_id=%s",
                clerk_user_id,
            )

    if current_choice == "byo_key" and provider_choice != "byo_key" and current_byo in ("openai", "anthropic"):
        from core.services.key_service import KeyService

        try:
            await KeyService().delete_key(clerk_user_id, current_byo)
        except Exception:
            logger.exception(
                "change_provider: delete_key failed for clerk_user_id=%s provider=%s",
                clerk_user_id,
                current_byo,
            )

    # Apply the swap to the running container.
    #
    # Two paths, picked by whether either side of the transition uses BYO:
    #
    # - **Cold path** (BYO involved on either side) — re-register the ECS
    #   task definition and force a new deployment. Required because BYOK
    #   injects ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` via task-def
    #   ``secrets: [{valueFrom: <SecretsManagerARN>}]`` resolved at task
    #   start; ECS does not let you mutate a running task's env vars.
    #   Ends the user's current chat session.
    #
    # - **Hot path** (Bedrock ↔ ChatGPT OAuth only) — Track-1 patch the
    #   three provider-block subtrees in openclaw.json (handled atomically
    #   by ``config_patcher.replace_provider_block``) and, for a switch
    #   *into* chatgpt_oauth, also pre-stage ``auth-profiles.json`` to EFS
    #   *before* the patch so chokidar's reload reads it in place.
    #   OpenClaw classifies all three keys as ``kind: "hot"`` in
    #   ``gateway/config-reload-plan.ts`` (``restart-heartbeat`` +
    #   ``reload-plugins``), so the gateway swaps providers without
    #   restarting. No task-def env-var change → no container restart →
    #   the chat session stays alive.
    #
    # On the hot path, if the owner has no ``openclaw.json`` on EFS yet
    # (legacy / un-provisioned owner), ``replace_provider_block`` raises
    # ``ConfigPatchError`` and we fall through to the cold path so the
    # file gets written from scratch by ``provision_user_container``.
    involves_byo = current_choice == "byo_key" or provider_choice == "byo_key"

    if not involves_byo:
        try:
            await _hot_swap_provider(
                owner_id=owner_id,
                clerk_user_id=clerk_user_id,
                new_provider=provider_choice,
            )
            put_metric(
                "billing.provider.changed",
                dimensions={
                    "from": current_choice or "none",
                    "to": provider_choice,
                    "path": "hot",
                },
            )
            return {
                "status": "ok",
                "provider_choice": provider_choice,
                "byo_provider": next_byo,
                "restart": False,
            }
        except _HotSwapUnavailableError:
            # Owner has no openclaw.json on EFS yet — fall through to the
            # cold path. logger inside _hot_swap_provider already recorded
            # the reason.
            pass

    put_metric(
        "billing.provider.changed",
        dimensions={
            "from": current_choice or "none",
            "to": provider_choice,
            "path": "cold",
        },
    )

    from core.services.container_provision import background_provision

    asyncio.create_task(background_provision(owner_id, clerk_user_id))

    return {
        "status": "ok",
        "provider_choice": provider_choice,
        "byo_provider": next_byo,
        "restart": True,
    }


class _HotSwapUnavailableError(Exception):
    """Internal — signals change_provider to fall back to the cold path."""


async def _hot_swap_provider(
    *,
    owner_id: str,
    clerk_user_id: str,
    new_provider: ProviderChoice,
) -> None:
    """Apply a Bedrock ↔ ChatGPT-OAuth provider switch hot, no container restart.

    Step order matters:
      1. If switching *to* chatgpt_oauth, write ``auth-profiles.json``
         to EFS FIRST. Once we patch openclaw.json (step 2) chokidar fires
         a reload and the openai-codex provider initializes — it needs the
         auth file already in place.
      2. Atomically swap the three openclaw.json subtrees via
         ``replace_provider_block``. OpenClaw's reload-plan picks up the
         change as ``kind: "hot"``.

    Switching *away* from chatgpt_oauth has the symmetric cleanup
    (``revoke_user_oauth`` + ``delete_auth_profile_store``) but that runs
    in the caller's side-effect cleanup block earlier — not here — so the
    stale auth file is already gone before we patch.

    Raises ``_HotSwapUnavailableError`` if the owner has no openclaw.json
    on EFS yet (legacy / un-provisioned), signaling the caller to fall
    back to the cold path.
    """
    from core.containers.config import build_provider_block
    from core.services.config_patcher import ConfigPatchError, replace_provider_block

    if new_provider == "chatgpt_oauth":
        # We already gated on tokens existing in the prerequisite check at
        # the top of change_provider. Re-read so the file we write reflects
        # whatever rotation may have happened concurrently.
        from core.containers.workspace import pre_stage_auth_profile_store
        from core.services import oauth_service

        tokens = await oauth_service.get_decrypted_tokens(user_id=clerk_user_id)
        if not tokens:
            # Lost the tokens between the prerequisite check and now (very
            # rare — concurrent disconnect). Bail out to the cold path so
            # the user lands on the oauth_required gate next instead of a
            # silently misconfigured container.
            logger.warning(
                "change_provider hot path: tokens disappeared between gate and pre-stage for user=%s",
                clerk_user_id,
            )
            raise _HotSwapUnavailableError("oauth_tokens_missing")
        await pre_stage_auth_profile_store(user_id=clerk_user_id, oauth_tokens=tokens)

    providers, default_model, plugin_entries = build_provider_block(
        provider_choice=new_provider,
        user_id=clerk_user_id,
        byo_provider=None,  # hot path never carries byo_key
    )

    try:
        await replace_provider_block(
            owner_id,
            providers=providers,
            default_model=default_model,
            plugin_entries=plugin_entries,
        )
    except ConfigPatchError as e:
        logger.info(
            "change_provider hot path: openclaw.json missing for owner=%s (%s) — falling back to cold path",
            owner_id,
            e,
        )
        raise _HotSwapUnavailableError("config_missing") from e
