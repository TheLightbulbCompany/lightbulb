"""Pydantic models for the Launch Competition HTTP surface."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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
    """The eight admin-editable launch-settings fields, as PUT by the admin
    console. Every field is REQUIRED, with no default: the write is a full
    replace, so a partial body has to be a 422 rather than a silent reset of
    whatever it left out — a body with no dates would otherwise blank the
    launch schedule. (``drop_at``/``competition_start_at`` are nullable, but
    the client still has to say ``null``.)

    ``referral_jump`` is the queue-jump setting: how many waitlist spots each
    referral moves the referrer up (``core.services.launch_service.
    effective_position``).

    The three ``stripe_*`` ids are deliberately NOT here: they are minted by
    the coupon endpoint and carried forward by
    ``core.services.launch_settings.update``, never client-supplied — which
    also keeps them out of the admin-action audit body. Real validation
    (ranges, date grammar, enabled-requires-both-dates) happens in
    ``core.services.launch_settings.validate``; this model exists to give the
    field list a shape, not to duplicate that logic."""

    enabled: bool
    drop_at: str | None
    competition_start_at: str | None
    comp_slots: int
    competition_days: int
    pot_front_cents: int
    pot_per_entrant_cents: int
    referral_jump: int


class StripeCouponCreateRequest(BaseModel):
    force: bool = False
