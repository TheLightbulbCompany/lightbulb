"""Pydantic schemas for billing API endpoints."""

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
