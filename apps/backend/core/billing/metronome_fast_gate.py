"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS instead. Fed by (1) optimistic decrement on
deplete() and (2) authoritative set from the low-balance webhook. Carries NO
money-correctness burden: Metronome is the SoR, and the IAM-revoke +
desired=0 kill-switch is the backstop if the flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, updated_at:str, [blocked:bool]}

``remaining_microcents`` is the authoritative cached number; ``blocked`` is a
convenience flag set only by the webhook ``set_balance`` (the synchronous gate
derives blocked-ness from ``remaining_microcents <= 0`` so an optimistic
``decrement`` needs no second write). An owner with no row is treated as NOT
blocked on the gate read (a brand-new owner shouldn't be gated by a missing
projection), but an optimistic decrement against a missing row starts from 0
and lands negative (a metered-but-unfunded owner IS gated).
"""

from __future__ import annotations

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


class MetronomeFastGate:
    """The synchronous chat gate's view of Metronome balance."""

    async def is_blocked(self, owner_id: str) -> bool:
        """True when the owner is out of spendable balance.

        Derived from ``remaining_microcents`` (the authoritative cached number),
        NOT from a separately-persisted ``blocked`` attribute — so an optimistic
        ``decrement`` that crosses zero blocks immediately without a second
        write. A missing row is NOT blocked (a brand-new owner shouldn't be
        gated by a missing projection).
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

    async def decrement(self, owner_id: str, *, microcents: int) -> int:
        """Optimistic local decrement on deplete. Returns the new remaining.

        ONE atomic update_item: ``ADD`` on a missing numeric attribute treats it
        as 0, so an unfunded owner lands negative. ``blocked`` is NOT persisted
        here — ``is_blocked`` derives it from the post-decrement value. A single
        write means a meter-Lambda DLQ redrive that re-runs ``decrement`` after a
        partial failure cannot double-decrement via a stranded second round trip
        (the only residual is the safe-direction over-block from the re-applied
        ADD, which the next webhook ``set_balance`` re-arms).
        """
        resp = _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="ADD remaining_microcents :neg SET updated_at = :now",
            ExpressionAttributeValues={":neg": -int(microcents), ":now": _now_iso()},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["remaining_microcents"])

    async def add_credit(self, owner_id: str, *, microcents: int) -> int:
        """Optimistic local re-arm on a CONFIRMED Stripe top-up. Returns the new
        remaining.

        Mirror of ``decrement``: one atomic ADD, ``blocked`` derived lazily.
        Needed because ``get_balance`` now reads this cache (not Metronome's
        async getNetBalance), so a paid top-up must bump the cache to unblock
        the owner's next message — the low-balance webhook only fires when
        balance crosses DOWN past a threshold, never on a top-up. Safe
        direction: the caller funds only inside Stripe's confirmed-payment
        branch, and the next low-balance webhook re-syncs the authoritative
        number (spec §3.3: "fund Metronome prepaid credit -> flag refreshed").
        """
        resp = _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="ADD remaining_microcents :pos SET updated_at = :now",
            ExpressionAttributeValues={":pos": int(microcents), ":now": _now_iso()},
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"]["remaining_microcents"])
