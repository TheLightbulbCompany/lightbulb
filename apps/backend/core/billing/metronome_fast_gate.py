"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS instead. The backend never depletes — usage
flows from the meter Lambda straight to Metronome — so the flag has exactly
two feeders: (1) authoritative set from the low-balance webhook, (2) the
gate's lazy ``getNetBalance`` refresh on a stale row (``is_stale``), which is
also what re-arms after a top-up (payment-gated commits confirm nothing
synchronously, and Metronome has no recovery webhook). Carries NO
money-correctness burden: Metronome is the SoR, and the IAM revoke is the
backstop if the flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, updated_at:str, [blocked:bool]}

``remaining_microcents`` is the authoritative cached number; ``blocked`` is a
convenience flag set only by ``set_balance`` (the synchronous gate derives
blocked-ness from ``remaining_microcents <= 0`` so an authoritative set
re-arm needs no second write). An owner with no row is treated as NOT blocked
on the gate read (a brand-new owner shouldn't be gated by a missing
projection).
"""

from __future__ import annotations

import calendar
import os
import time
from typing import Any

import boto3

from core.config import settings
from core.dynamodb import run_in_thread


def _table_name() -> str:
    name = os.environ.get("METRONOME_FAST_GATE_TABLE") or settings.METRONOME_FAST_GATE_TABLE
    if not name:
        raise RuntimeError("METRONOME_FAST_GATE_TABLE is empty — fast-gate is misconfigured.")
    return name


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# This module sits on the synchronous chat hot path, so the boto3 Table
# resource is built once and cached (per resolved name — tests monkeypatch
# the env var) instead of reconstructed per call. The repo-wide
# core.dynamodb.get_table helper isn't usable here because it derives names
# from DYNAMODB_TABLE_PREFIX, while this table's full name comes from its
# own env var (wired by service-stack.ts).
_table_cache: dict[str, Any] = {}


def _table():
    name = _table_name()
    table = _table_cache.get(name)
    if table is None:
        table = boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(name)
        _table_cache[name] = table
    return table


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
        NOT from a separately-persisted ``blocked`` attribute — so any write
        that crosses zero flips blocked-ness without a second write. A missing
        row is NOT blocked (a brand-new owner shouldn't be gated by a missing
        projection).
        """
        resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id}, ConsistentRead=True)
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
        resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id}, ConsistentRead=True)
        item = resp.get("Item")
        if not item:
            return 0
        remaining = item.get("remaining_microcents")
        return int(remaining) if remaining is not None else 0

    async def get_row(self, owner_id: str) -> dict | None:
        """The raw projection row (or None), for callers that need more than
        the derived blocked-ness — e.g. the lazy-refresh consumer feeding
        ``is_stale`` with the row's ``updated_at``."""
        resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id}, ConsistentRead=True)
        return resp.get("Item")

    async def set_balance(self, owner_id: str, *, remaining_microcents: int) -> None:
        """Authoritative set (from a Metronome webhook or a fresh balance read).

        This is the ONLY writer: top-ups are payment-gated Metronome commits
        with no synchronous confirmation, so there is no optimistic local
        credit bump — the gate's lazy balance refresh observes the released
        credits and re-arms via this same authoritative set.
        """
        await run_in_thread(
            _table().put_item,
            Item={
                "owner_id": owner_id,
                "remaining_microcents": int(remaining_microcents),
                "blocked": remaining_microcents <= 0,
                "updated_at": _now_iso(),
            },
        )
