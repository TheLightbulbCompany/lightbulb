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
    managed_inference_available: bool = False
    # Pricing tier: "standard" (Workspace) or "ultra_pro" (Ultra). Defaults to
    # "standard" so a billing row created before the plan field existed (no
    # `plan` key) surfaces cleanly rather than as null.
    plan: str = "standard"
    # Server-owned workspace price display. Clients must not bake this into a
    # binary because a catalog update would otherwise leave stale checkout
    # copy in the field.
    workspace_price_usd: int
    base_price_usd: int
    ultra_price_usd: int
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
