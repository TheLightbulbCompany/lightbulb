"""launch-settings table — a single row (``id="settings"``) holding the Launch
Competition's human inputs: the two dates, the pot constants, comp slots, and
the Stripe free-month coupon/promo ids. Edited from the admin console
(``core.services.launch_settings``); read by ``core.services.launch_phase``
through a 30-second in-process cache.

``DEFAULTS`` is the row's shape before any admin has ever saved it (feature
off — see ``launch_phase.schedule()``'s None contract).
"""

from __future__ import annotations

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_ID = "settings"

DEFAULTS: dict = {
    "enabled": False,
    "drop_at": None,
    "competition_start_at": None,
    "comp_slots": 220,
    "competition_days": 30,
    "pot_front_cents": 100_000,
    "pot_per_entrant_cents": 1_000,
    "stripe_coupon_id": "",
    "stripe_promotion_code_id": "",
    "stripe_promotion_code": "",
}


def _table():
    return get_table("launch-settings")


def _plain(item: dict | None) -> dict | None:
    if not item:
        return None
    out = dict(item)
    out.pop("id", None)
    return out


async def get() -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"id": _ID})
    return _plain(resp.get("Item"))


def get_sync() -> dict | None:
    """Sync twin of ``get`` for ``launch_phase.schedule()``, which stays sync
    because the container-status poll path calls it synchronously.
    ``run_in_thread`` is for async callers only — it has no meaning called
    from a function that isn't itself a coroutine."""
    resp = _table().get_item(Key={"id": _ID})
    return _plain(resp.get("Item"))


async def put(item: dict) -> dict:
    """Full replace of the settings row. Stamps ``updated_at``; ``updated_by``
    must already be on ``item`` — the caller (``launch_settings.update``)
    is the one that knows who's writing."""
    row = dict(item)
    row["id"] = _ID
    row["updated_at"] = utc_now_iso()
    await run_in_thread(_table().put_item, Item=row)
    return _plain(row)
