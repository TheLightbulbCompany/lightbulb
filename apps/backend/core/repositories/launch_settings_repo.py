"""launch-settings table — a single row (``id="settings"``) holding the Launch
Competition's human inputs: the two dates, the pot constants, comp slots, and
the Stripe free-month coupon/promo ids. Edited from the admin console
(``core.services.launch_settings``); read by ``core.services.launch_phase``
through a 30-second in-process cache.

``DEFAULTS`` is the row's shape before any admin has ever saved it (feature
off — see ``launch_phase.schedule()``'s None contract).
"""

from __future__ import annotations

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import get_table, run_in_thread, table_name, utc_now_iso

_ID = "settings"
_INT_FIELDS = ("comp_slots", "competition_days", "pot_front_cents", "pot_per_entrant_cents", "mint_nonce")

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
    # Server-only — never exposed via `launch_settings.to_public` or the
    # admin PUT schema. Bumped by `create_stripe_coupon` on every successful
    # mint so coupon/promo idempotency keys (and the promo code itself) are a
    # pure function of the row instead of a fresh random value per call —
    # see the fix-round note in `core/services/launch_settings.py`.
    "mint_nonce": 0,
}


def _table():
    return get_table("launch-settings")


# `get_sync` runs on the container-status poll path (`launch_phase._row`),
# where an unreachable DynamoDB has to fail FAST — botocore's defaults would
# park the request for 60s a try. Same table name, region and endpoint as
# `get_table("launch-settings")` resolves (`core.dynamodb`); only the timeouts
# differ, which is why this is a module-local resource rather than the shared
# one.
_SYNC_CONFIG = Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 2})
_sync_resource = None


def _sync_table():
    global _sync_resource
    if _sync_resource is None:
        endpoint = getattr(settings, "DYNAMODB_ENDPOINT_URL", None)
        _sync_resource = boto3.resource(
            "dynamodb",
            region_name=getattr(settings, "AWS_REGION", "us-east-1"),
            config=_SYNC_CONFIG,
            **({"endpoint_url": endpoint} if endpoint else {}),
        )
    return _sync_resource.Table(table_name("launch-settings"))


def _plain(item: dict | None) -> dict | None:
    if not item:
        return None
    out = dict(item)
    out.pop("id", None)
    for k in _INT_FIELDS:
        if out.get(k) is not None:
            out[k] = int(out[k])
    if out.get("enabled") is not None:
        # Strict identity, not `bool(...)` — `bool("false")` is True in
        # Python, so a hand-edited string value must not enable the launch.
        out["enabled"] = out["enabled"] is True
    return out


async def get() -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"id": _ID})
    return _plain(resp.get("Item"))


def get_sync() -> dict | None:
    """Sync twin of ``get`` for ``launch_phase.schedule()``, which stays sync
    because the container-status poll path calls it synchronously.
    ``run_in_thread`` is for async callers only — it has no meaning called
    from a function that isn't itself a coroutine. Reads through the bounded
    client above, because this one blocks the event loop while it waits."""
    resp = _sync_table().get_item(Key={"id": _ID})
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
