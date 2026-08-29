"""Pydantic models for the Launch Competition HTTP surface."""

from __future__ import annotations

from pydantic import BaseModel, Field


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


class EnterRequest(BaseModel):
    display_name: str = Field(min_length=2, max_length=40)
    stripe_key: str = Field(min_length=20, max_length=200)
    public: bool = True


class LeaderboardRow(BaseModel):
    rank: int
    display_name: str
    delta_cents: int
    pct: float | None
    currency: str


class LeaderboardResponse(BaseModel):
    entrants: int
    rows: list[LeaderboardRow]
