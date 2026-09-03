"""Pydantic models for the Launch Competition HTTP surface."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class LaunchInfo(BaseModel):
    """The public /launch payload. NO pot fields, ever — the pot is
    admin-only (it lives in the ops sheet payload)."""

    phase: str
    sheet_closes_at: str
    competition_start_at: str
    competition_end_at: str
    signups: int
    comp_slots: int
    referral_jump: int


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


# The one seat ceiling — the admin edit path validates against it too
# (``core.services.launch_service.set_team_entry``), so it lives here rather
# than as a literal in each.
TEAM_SEATS_MAX = 500


class TeamInterestRequest(BaseModel):
    """A team raising its hand — NOT an entry. The founders enter teams by
    hand, so this body is only what they need to have that conversation."""

    team_name: str = Field(min_length=2, max_length=80)
    seats: int = Field(ge=1, le=TEAM_SEATS_MAX)
    note: str = Field(default="", max_length=1000)

    @field_validator("team_name", "note", mode="before")
    @classmethod
    def _strip_text(cls, v):
        # Before, not after: a whitespace-only team name has to FAIL
        # min_length rather than pass it and land as "".
        return v.strip() if isinstance(v, str) else v


class TeamEntryPatchRequest(BaseModel):
    """The admin's edit of one team entry: correct the seat count, and decide
    whether it counts toward the pot. Both optional — send what changed."""

    seats: int | None = None
    counted: bool | None = None


class ConnectStripeRequest(BaseModel):
    stripe_key: str = Field(min_length=20, max_length=200)


class EntryPatchRequest(BaseModel):
    public: bool


class DisplayNamePatchRequest(BaseModel):
    """Rename on the sheet after joining. Same bounds and same
    strip-before-validate as ``JoinRequest`` so a name can never be renamed
    into something ``join`` would have rejected."""

    display_name: str = Field(min_length=2, max_length=40)

    @field_validator("display_name", mode="before")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip() if isinstance(v, str) else v


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
    """The nine admin-editable launch-settings fields, as PUT by the admin
    console. Every field is REQUIRED, with no default: the write is a full
    replace, so a partial body has to be a 422 rather than a silent reset of
    whatever it left out — a body with no dates would otherwise blank the
    launch schedule. (The three dates are nullable, but the client still has
    to say ``null``.)

    ``referral_jump`` is the queue-jump setting: how many sheet spots each
    counted referral (referee subscribed) moves the referrer up
    (``core.services.launch_service.effective_position``).

    ``stripe_coupon_id`` and the close-state fields (``closed_at``,
    ``final_pot_cents``, ``final_roster_count``) are deliberately NOT here:
    they are written by the coupon mint / close action and carried forward
    by ``core.services.launch_settings.update``, never client-supplied.
    Real validation (ranges, date grammar, orderings, enabled-requires-dates)
    happens in ``core.services.launch_settings.validate``; this model exists
    to give the field list a shape, not to duplicate that logic."""

    enabled: bool
    sheet_closes_at: str | None
    competition_start_at: str | None
    competition_end_at: str | None
    comp_slots: int
    pot_front_cents: int
    pot_per_entrant_cents: int
    # Per SEAT of a counted team entry, on top of pot_per_entrant_cents. It
    # MUST be listed here: the router does `body.model_dump()`, so a field the
    # model does not declare is dropped by pydantic before validate() ever
    # sees it, `validate` falls back to DEFAULTS, and `put` — a FULL replace —
    # rewrites the stored value on every save. A field missing from this model
    # is a field the admin form cannot change.
    pot_per_seat_cents: int
    referral_jump: int


class LaunchEmailCopyUpdate(BaseModel):
    """One variant's copy override. Every field optional and BLANK-MEANS-
    DEFAULT: the shipped copy in ``core.services.launch_sheet_email`` is the
    source of truth, the row only ever holds overrides, so clearing all three
    is how "reset to default" is sent. Plain text — it is escaped into the
    HTML shell, never rendered as markup."""

    variant: str
    subject: str | None = None
    lead: str | None = None
    detail: str | None = None


class LaunchEmailTestRequest(BaseModel):
    """Send one variant to the signed-in admin's own Clerk address, with
    sample values. Touches no sheet row."""

    variant: str


class StripeCouponCreateRequest(BaseModel):
    force: bool = False


class LaunchCloseRequest(BaseModel):
    """``force`` re-runs an already-stamped close so a partial failure
    (Stripe blip mid coupon-attach, an email that never sent) can converge —
    every step is idempotent and skips work already done."""

    force: bool = False
