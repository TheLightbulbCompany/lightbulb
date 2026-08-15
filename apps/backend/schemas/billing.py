"""Pydantic schemas for billing API endpoints."""

from typing import Literal

from pydantic import BaseModel


class BillingAccountResponse(BaseModel):
    is_subscribed: bool
    current_spend: float
    lifetime_spend: float
    # Stripe-native subscription state. Frontend TrialBanner reads
    # `subscription_status` + `trial_end` to render the days-left
    # countdown. Both null until the user signs up.
    subscription_status: str | None = None
    trial_end: int | None = None  # Unix epoch seconds
    # provider_choice lives on billing_accounts (per-owner), not on the
    # user row. Frontend reads these here to determine whether to show
    # ProviderPicker (skip when set) and which provider's settings panel
    # to render.
    provider_choice: str | None = None
    byo_provider: str | None = None
    # Pricing tier: "standard" (the single $50/mo tier) or "ultra_pro" (the
    # higher tier that unlocks larger marketplace companies). Defaults to
    # "standard" so a billing row created before the plan field existed (no
    # `plan` key) surfaces cleanly rather than as null.
    plan: str = "standard"
    # Teams (org) per-seat billing. seat_count is the live org member count for
    # org owners (drives the paywall's "N × $20" math); team_seat_price_usd is
    # the single server-side source of the per-seat display price. Both None
    # for personal owners.
    seat_count: int | None = None
    team_seat_price_usd: int | None = None
    # Flat monthly base charged alongside the per-seat item. None for personal
    # owners. Frontend renders "$50 + N x $20" from these two numbers.
    team_base_price_usd: int | None = None
    # Personal flat-plan monthly price in whole dollars. Not owner-dependent,
    # so always populated.
    #
    # Load-bearing for the iOS paywall, which shows the web price beside
    # Apple's: App Review permits that on the U.S. storefront but requires the
    # figure to be ACCURATE — a stale one is a 5.1.1 violation, not just bad
    # copy. A number baked into the binary would go wrong the moment pricing
    # changed and could only be fixed by shipping a build, so the server owns
    # it.
    flat_price_usd: int | None = None
    # Which channel the live subscription was bought through: "apple_iap", or
    # absent/None for the Stripe path (the default, and every pre-IAP row).
    #
    # Exists so a client can send the customer to the RIGHT place to manage or
    # cancel. An Apple subscription is managed only in iOS Settings — Stripe
    # has no record of it, so the customer portal would be a dead end — while a
    # Stripe subscription cannot be touched from iOS Settings. Apple's
    # guidelines expect an app selling auto-renewable subscriptions to point at
    # the former; getting this backwards strands a paying customer trying to
    # cancel, which is the complaint that generates refunds and 1-star reviews.
    subscription_source: str | None = None
    # True only for org owners on a REAL per-seat Stripe subscription (one
    # that recorded subscription_item_id via the webhook). False for
    # personal owners and for flat/comped/grandfathered org subs that have
    # no subscription_item_id — those must never render seat-priced
    # "N x $price" billing math (there is no per-seat Stripe item to price).
    is_seat_subscription: bool = False


class PortalResponse(BaseModel):
    portal_url: str


class MemberUsage(BaseModel):
    user_id: str
    display_name: str | None = None
    email: str | None = None
    total_spend: float
    total_input_tokens: int
    total_output_tokens: int
    request_count: int


class UsageSummary(BaseModel):
    period: str
    total_spend: float
    total_input_tokens: int
    total_output_tokens: int
    total_cache_read_tokens: int
    total_cache_write_tokens: int
    request_count: int
    lifetime_spend: float
    by_member: list[MemberUsage] = []


class ModelPriceResponse(BaseModel):
    input: float
    output: float
    cache_read: float
    cache_write: float


class MyUsageResponse(BaseModel):
    period: str
    total_spend: float
    total_input_tokens: int
    total_output_tokens: int
    request_count: int


class PricingResponse(BaseModel):
    models: dict[str, ModelPriceResponse]


class SpendByModel(BaseModel):
    model: str
    tier: str
    cents: int


class SpendQuotaWindow(BaseModel):
    label: str
    used_percent: float
    resets_at_ms: int | None = None


class SpendResponse(BaseModel):
    """Provider-aware spend summary. `mode` picks the render variant:
    bedrock -> billed fields; oauth_quota -> plan/windows; tokens_only -> neither."""

    mode: Literal["bedrock", "oauth_quota", "tokens_only"]
    billed_month_to_date_cents: int | None = None
    by_model: list[SpendByModel] | None = None
    period_start: str | None = None
    plan: str | None = None
    windows: list[SpendQuotaWindow] | None = None
