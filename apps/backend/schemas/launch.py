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
