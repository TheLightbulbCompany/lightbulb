"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS instead. Fed by (1) optimistic decrement on
deplete() and (2) authoritative set from the low-balance webhook. Carries NO
money-correctness burden: Metronome is the SoR, and the IAM-revoke +
desired=0 kill-switch is the backstop if the flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, blocked:bool, updated_at:str}

An owner with no row is treated as NOT blocked on the gate read (a brand-new
owner shouldn't be gated by a missing projection), but an optimistic
decrement against a missing row starts from 0 and lands negative+blocked
(a metered-but-unfunded owner IS gated).
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
        resp = _table().get_item(Key={"owner_id": owner_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return False
        return bool(item.get("blocked", False))

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

        ADD on a missing numeric attribute treats it as 0, so an unfunded
        owner lands negative. Re-derives `blocked` from the post-decrement
        value in the same update.
        """
        resp = _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="ADD remaining_microcents :neg SET updated_at = :now",
            ExpressionAttributeValues={":neg": -int(microcents), ":now": _now_iso()},
            ReturnValues="UPDATED_NEW",
        )
        new_remaining = int(resp["Attributes"]["remaining_microcents"])
        _table().update_item(
            Key={"owner_id": owner_id},
            UpdateExpression="SET blocked = :b",
            ExpressionAttributeValues={":b": new_remaining <= 0},
        )
        return new_remaining
