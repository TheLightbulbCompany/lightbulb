"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS instead. The backend never depletes — usage
flows from the meter Lambda straight to Metronome — so the flag has exactly
two feeders: (1) authoritative set from the low-balance webhook, (2) re-arm
on a confirmed Stripe top-up. A later consumer adds a lazy ``getNetBalance``
poll on gate-read when the row is stale (``is_stale``), because Metronome has
no verified recovery webhook. Carries NO money-correctness burden: Metronome
is the SoR, and the IAM-revoke + desired=0 kill-switch is the backstop if the
flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, updated_at:str, [blocked:bool]}

``remaining_microcents`` is the authoritative cached number; ``blocked`` is a
convenience flag set only by ``set_balance`` (the synchronous gate derives
blocked-ness from ``remaining_microcents <= 0`` so the atomic ``add_credit``
re-arm needs no second write). An owner with no row is treated as NOT blocked
on the gate read (a brand-new owner shouldn't be gated by a missing
projection).
"""

from __future__ import annotations

import calendar
import os
import time

import boto3

from core.config import settings


def _table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — fast-gate is misconfigured.")
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_table_name())


def is_stale(row: dict | None, max_age_s: int) -> bool:
    """True when the cached projection can't be trusted for a lazy refresh.

    Compares the row's ``updated_at`` (the ``_now_iso`` format this module
    writes) against now. A missing row, missing timestamp, or a timestamp
    that doesn't parse all count as stale — the safe answer for a refresh
    decision is "go ask Metronome", never "trust the garbled cache".
    """
    if not row:
        return True
    updated_at = row.get("updated_at")
    if not isinstance(updated_at, str):
        return True
    try:
        updated_epoch = calendar.timegm(time.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return True
    return (time.time() - updated_epoch) > max_age_s


class MetronomeFastGate:
    """The synchronous chat gate's view of Metronome balance."""

    async def is_blocked(self, owner_id: str) -> bool:
        """True when the owner is out of spendable balance.

        Derived from ``remaining_microcents`` (the authoritative cached number),
        NOT from a separately-persisted ``blocked`` attribute — so an atomic
        ``add_credit`` re-arm that crosses zero unblocks immediately without a
        second write. A missing row is NOT blocked (a brand-new owner shouldn't
        be gated by a missing projection).
        """
        resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return False
        remaining = item.get("remaining_microcents")
        if remaining is not None:
            return int(remaining) <= 0
        # No remaining number on the row (shouldn't happen post-Phase-2, but be
        # defensive): fall back to a legacy persisted flag if present.
        return bool(item.get("blocked", False))

    async def get_remaining(self, owner_id: str) -> int:
        """The cached remaining balance (microcents) the synchronous chat /
        provision gate reads instead of Metronome's async, no-SLA getNetBalance.

        Returns 0 for a missing row. The gate's hard-block is keyed off
        ``is_blocked`` (which treats a missing row as not-blocked), so this 0
        is only an informational balance for an as-yet-unmetered owner.
        """
        resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return 0
        remaining = item.get("remaining_microcents")
        return int(remaining) if remaining is not None else 0

    async def get_row(self, owner_id: str) -> dict | None:
        """The raw projection row (or None), for callers that need more than
        the derived blocked-ness — e.g. the lazy-refresh consumer feeding
        ``is_stale`` with the row's ``updated_at``."""
        resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
        return resp.get("Item")

    async def set_balance(self, owner_id: str, *, remaining_microcents: int) -> None:
        """Authoritative set (from a Metronome webhook or a fresh balance read)."""
        _table().put_item(
            Item={
                "owner_id": owner_id,
                "remaining_microcents": int(remaining_microcents),
                "blocked": remaining_microcents <= 0,
                "updated_at": _now_iso(),
            }
        )

    async def add_credit(self, owner_id: str, *, microcents: int) -> int:
        """Optimistic local re-arm on a CONFIRMED Stripe top-up. Returns the new
        remaining.

        One atomic ADD (a missing numeric attribute is treated as 0), with
        ``blocked`` derived lazily by ``is_blocked``. Needed because the gate
        reads this cache (not Metronome's async getNetBalance), so a paid
        top-up must bump the cache to unblock the owner's next message — the
        low-balance webhook only fires when balance crosses DOWN past a
        threshold, never on a top-up. Safe direction: the caller funds only
        inside Stripe's confirmed-payment branch, and the next low-balance
        webhook re-syncs the authoritative number (spec §3.3: "fund Metronome
        prepaid credit -> flag refreshed").
        """
        resp = _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="ADD remaining_microcents :pos SET updated_at = :now",
            ExpressionAttributeValues={":pos": int(microcents), ":now": _now_iso()},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["remaining_microcents"])
