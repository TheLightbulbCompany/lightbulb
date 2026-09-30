"""Local fast-gate flag — a disposable DDB projection of Metronome balance.

Metronome's balance read is async (no low-latency SLA, research §3), so the
synchronous chat gate reads THIS projection instead of calling Metronome
directly. Two debiters lower it provisionally: the openrouter-settlement
Lambda (inference) and the backend's voice-call close
(``voice_calls._bill``); the next refresh overwrites both. Two refreshers
set it authoritatively: (1) the low-balance webhook, (2) the gate's lazy
``getNetBalance`` refresh on a stale row (``is_stale``), which is also what re-arms after a
top-up (payment-gated commits confirm nothing synchronously, and Metronome
has no recovery webhook). Carries NO money-correctness burden: Metronome is
the SoR, and the IAM revoke is the backstop if the flag goes stale.

Item shape (PK owner_id):
  {owner_id, remaining_microcents:int, updated_at:str, [blocked:bool]}

This module is storage only — it has no blocked-ness opinion. The one
blocked-ness predicate lives in ``core.services.metronome_gate._row_blocked``,
which blocks at ``remaining_microcents <= RESERVE_ESTIMATE_MICROCENTS`` (NOT
``<= 0``) to mirror the bedrock-gate's own admission rule. Do not derive
blocked-ness here or anywhere else — a ``<= 0`` check admits an owner the
bedrock-gate will then 429.
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
    """Storage layer for the DDB fast-gate projection — no blocked-ness opinion.

    The blocked-ness predicate lives in
    ``core.services.metronome_gate._row_blocked``, which blocks at
    ``remaining_microcents <= RESERVE_ESTIMATE_MICROCENTS``, NOT ``<= 0``.
    Callers that need blocked-ness must go through that predicate (or
    ``is_owner_blocked``), never derive it from a raw balance read here.
    """

    async def get_row(self, owner_id: str) -> dict | None:
        """The raw projection row (or None), for callers that need more than
        the derived blocked-ness — e.g. the lazy-refresh consumer feeding
        ``is_stale`` with the row's ``updated_at``."""
        resp = await run_in_thread(_table().get_item, Key={"owner_id": owner_id}, ConsistentRead=True)
        return resp.get("Item")

    async def delete_row(self, owner_id: str) -> None:
        """Remove the owner's projection row — the workspace wipe's billing layer only."""
        await run_in_thread(_table().delete_item, Key={"owner_id": owner_id})

    async def set_balance(self, owner_id: str, *, remaining_microcents: int) -> None:
        """Authoritative set (from a Metronome webhook or a fresh balance read).

        This is the only authoritative SET of ``remaining_microcents`` (settlement
        and voice closes ADD provisional debits): top-ups are
        payment-gated Metronome commits with no synchronous confirmation, so
        there is no optimistic local credit bump — the gate's lazy balance
        refresh observes the released credits and re-arms via this same
        authoritative set.

        UPDATE (not put_item): the bedrock-gate Lambda keeps a live
        ``reserved_microcents`` hold field on this SAME row. A whole-row
        put_item would delete an outstanding hold mid-request, so a later
        settle would drive it negative and over-admit paid credits. SET only
        the three fields this method owns and leave ``reserved_microcents``
        untouched.
        """
        await run_in_thread(
            _table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET remaining_microcents = :r, blocked = :b, updated_at = :u",
            ExpressionAttributeValues={
                ":r": int(remaining_microcents),
                ":b": remaining_microcents <= 0,
                ":u": _now_iso(),
            },
        )
