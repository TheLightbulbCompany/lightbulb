"""Pydantic models for the Launch Competition HTTP surface."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from core.repositories.launch_settings_repo import DEFAULTS as _SETTINGS_DEFAULTS


class LaunchInfo(BaseModel):
    phase: str
    drop_at: str
    competition_start_at: str
    competition_end_at: str
    signups: int
    competing: int
    on_board: int
    pot_cents: int
    comp_slots: int
    pot_per_entrant_cents: int


class ReferralAttributeRequest(BaseModel):
    ref: str = Field(min_length=4, max_length=16)


class JoinRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=40)
    public: bool = True

    @field_validator("display_name", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        # Before, not after: the stored name is stripped anyway
        # (launch_service.join), so a whitespace-only name has to fail the
        # min_length check rather than pass it and land as "".
        return v.strip() if isinstance(v, str) else v


class ConnectStripeRequest(BaseModel):
    stripe_key: str = Field(min_length=20, max_length=200)


class EntryPatchRequest(BaseModel):
    public: bool


class LeaderboardRow(BaseModel):
    rank: int
    display_name: str
    delta_cents: int
    pct: float | None
    currency: str


class LeaderboardResponse(BaseModel):
    competing: int
    on_board: int
    rows: list[LeaderboardRow]


class LaunchSettingsUpdate(BaseModel):
    """The public launch-settings row, as edited from the admin console. A
    full replace (``PUT``) — defaults mirror ``launch_settings_repo.DEFAULTS``
    so an admin-omitted field lands on the same default the row would have
    anyway, rather than drifting from a second hardcoded copy. Real
    validation (ranges, date grammar, enabled-requires-both-dates) happens in
    ``core.services.launch_settings.validate`` — this model exists to give
    the field list a shape, not to duplicate that logic."""

    enabled: bool = _SETTINGS_DEFAULTS["enabled"]
    drop_at: str | None = _SETTINGS_DEFAULTS["drop_at"]
    competition_start_at: str | None = _SETTINGS_DEFAULTS["competition_start_at"]
    comp_slots: int = _SETTINGS_DEFAULTS["comp_slots"]
    competition_days: int = _SETTINGS_DEFAULTS["competition_days"]
    pot_front_cents: int = _SETTINGS_DEFAULTS["pot_front_cents"]
    pot_per_entrant_cents: int = _SETTINGS_DEFAULTS["pot_per_entrant_cents"]
    stripe_coupon_id: str = _SETTINGS_DEFAULTS["stripe_coupon_id"]
    stripe_promotion_code_id: str = _SETTINGS_DEFAULTS["stripe_promotion_code_id"]
    stripe_promotion_code: str = _SETTINGS_DEFAULTS["stripe_promotion_code"]


class StripeCouponCreateRequest(BaseModel):
    force: bool = False
