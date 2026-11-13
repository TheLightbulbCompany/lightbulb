"""Pydantic schemas for billing API endpoints."""

from typing import Literal

from pydantic import BaseModel


class Allowance(BaseModel):
    used_usd: float
    limit_usd: float
    resets_at: int  # epoch seconds, next 00:00 UTC


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
    # Pricing tier: "base", "standard" (Workspace) or "ultra_pro" (Ultra). A paid
    # row created before the plan field existed reads "standard"; a trial_local
    # row or no row at all has no plan and reads null.
    plan: str | None = None
    # Server-owned workspace price display. Clients must not bake this into a
    # binary because a catalog update would otherwise leave stale checkout
    # copy in the field.
    workspace_price_usd: int
    # Default keeps pre-Base billing rows/API callers backward compatible;
    # legacy callers may omit this field safely.
    # the router still supplies the server-owned configured value when present.
    base_price_usd: int = 20
    ultra_price_usd: int
    # The one plan (T06): workspace_price_usd covers ``included_members`` people,
    # then seat_price_usd a month for each member past them.
    seat_price_usd: int
    included_members: int
    # The Front desk add-on (a texting number): whether this workspace has it, and its monthly price.
    front_desk: bool = False
    front_desk_price_usd: int | None = None
    # Managed-credit markup (provider price x this), from openrouter-pricing.json via CDK.
    managed_markup: str | None = None
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
    # App Store product ids the iOS app may offer. Empty unless APPLE_IAP_SELLING:
    # the app renders no paywall when this is empty, which keeps IAP dark.
    apple_iap_product_ids: list[str] = []
    # True while the openrouter-gate's per-key daily limit has tripped today (UTC); clears at the
    # rollover. Lets the Model & Credits screen say "Daily allowance used" instead of "Active".
    capped_today: bool = False
    # Capped owners only: today's managed-model spend vs the key's daily limit, from OpenRouter.
    allowance: Allowance | None = None
    # Why this owner's agents cannot run right now, or None: "trial_expired", "trial_daily_cap",
    # "included_daily_cap" or "out_of_credits" (chat_gate.budget_block_reason). Drives the
    # messaging shell's limit bar; a client never re-derives it from the fields above.
    block_reason: str | None = None
    # Epoch seconds the workspace's own provider plan (ChatGPT, BYO key) is back from a usage
    # limit; null once passed. Display only -- the Model & Credits card says so until then.
    provider_quota_until: int | None = None


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
    capture_prompt_tokens: int = 0
    capture_completion_tokens: int = 0


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


class SpendResponse(BaseModel):
    """Spend summary. Always ``tokens_only`` now: the panel keeps its token grid. The Bedrock
    billed-dollars card and ChatGPT's plan-quota windows (the Hermes bridge never returned
    them) are both gone."""

    mode: Literal["tokens_only"]
