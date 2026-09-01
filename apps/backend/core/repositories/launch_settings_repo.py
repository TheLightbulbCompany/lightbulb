"""launch-settings table — a single row (``id="settings"``) holding the Launch
Competition's human inputs (the three dates, the pot constants, comp slots)
plus the server-managed close state and the Stripe free-month coupon id.
Edited from the admin console (``core.services.launch_settings``); read by
``core.services.launch_phase`` through a 30-second in-process cache.

``DEFAULTS`` is the row's shape before any admin has ever saved it (feature
off — see ``launch_phase.schedule()``'s None contract).
"""

from __future__ import annotations

from decimal import Decimal

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import get_table, run_in_thread, table_name, utc_now_iso

_ID = "settings"
_INT_FIELDS = (
    "comp_slots",
    "pot_front_cents",
    "pot_per_entrant_cents",
    "pot_per_seat_cents",
    "referral_jump",
    "mint_nonce",
    "final_pot_cents",
    "final_roster_count",
)

DEFAULTS: dict = {
    "enabled": False,
    "sheet_closes_at": None,
    "competition_start_at": None,
    "competition_end_at": None,
    "comp_slots": 200,
    "pot_front_cents": 100_000,
    "pot_per_entrant_cents": 1_000,
    # Team entries (core.repositories.launch_teams_repo) contribute the
    # per-entrant amount PLUS this much per seat — a team is one entrant with
    # a size. Counted teams only; see launch_phase.pot_cents.
    "pot_per_seat_cents": 500,
    # Sheet queue-jump: each counted referral (referee subscribed) moves the
    # referrer up this many spots (see launch_service.effective_position).
    "referral_jump": 10,
    # Close state — written by core.services.launch_close, never by the admin
    # settings PUT (carried forward server-side like the coupon fields).
    "closed_at": None,
    "final_pot_cents": None,
    "final_roster_count": None,
    # Async close bookkeeping (routers/ops.py POST /launch/close → 202):
    # started-stamp + the finished run's summary counts.
    "close_started_at": None,
    "close_result": None,
    "stripe_coupon_id": "",
    # Per-variant email copy OVERRIDES ({variant: {subject/lead/detail}}) —
    # empty means every email uses the shipped default in
    # `core.services.launch_sheet_email`. Written by the admin console's
    # email-copy endpoint, carried forward by the settings PUT.
    "email_copy": {},
    # Server-only — never exposed via the admin PUT schema. Bumped by
    # `create_stripe_coupon` on every forced re-mint so the coupon idempotency
    # key is a pure function of the row instead of a fresh random value per
    # call — see the note in `core/services/launch_settings.py`.
    "mint_nonce": 0,
}


# The pot freeze is write-once at the DB level. It cannot be a bare
# `attribute_not_exists`: rows saved before `put` learned to omit None-valued
# fields carry `final_pot_cents` as a DynamoDB NULL, so the attribute EXISTS
# and the freeze would be rejected — silently — on every close. A NULL means
# "never frozen", exactly like an absent attribute.
POT_UNFROZEN = "attribute_not_exists(final_pot_cents) OR attribute_type(final_pot_cents, :nulltype)"
POT_UNFROZEN_VALUES = {":nulltype": "NULL"}


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
    if isinstance(out.get("close_result"), dict):
        # The summary counts come back as Decimals like every DynamoDB number.
        out["close_result"] = {k: int(v) if isinstance(v, Decimal) else v for k, v in out["close_result"].items()}
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


async def update_fields(fields: dict, *, condition: str | None = None, condition_values: dict | None = None) -> bool:
    """SET only the supplied fields on the settings row — one atomic
    UpdateExpression, no read-modify-write, so a close-state write can never
    clobber a concurrent admin save (and vice versa). Upserts the row if no
    admin ever saved one (the DEFAULTS merge covers the rest).

    ``condition`` is an optional ConditionExpression (``condition_values``
    supplies any placeholders it names — see ``POT_UNFROZEN``); a failed
    condition is swallowed and returns False — DB-level enforcement of
    write-once fields (the pot freeze) under genuine concurrency."""
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    values: dict = {f":v{i}": v for i, v in enumerate(fields.values())}
    values[":t"] = utc_now_iso()
    kwargs: dict = {
        "Key": {"id": _ID},
        "UpdateExpression": "SET " + ", ".join(f"{n} = :v{i}" for i, n in enumerate(names)) + ", updated_at = :t",
        "ExpressionAttributeNames": names,
        "ExpressionAttributeValues": values,
    }
    if condition:
        kwargs["ConditionExpression"] = condition
        values.update(condition_values or {})
    try:
        await run_in_thread(_table().update_item, **kwargs)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def put(item: dict) -> dict:
    """Full replace of the settings row. Stamps ``updated_at``; ``updated_by``
    must already be on ``item`` — the caller (``launch_settings.update``)
    is the one that knows who's writing."""
    row = dict(item)
    row["id"] = _ID
    row["updated_at"] = utc_now_iso()
    # None-valued fields are OMITTED, never written. DynamoDB stores a None as
    # a NULL attribute, which EXISTS — and the close's write-once pot freeze
    # keys off the attribute NOT existing, so persisting the DEFAULTS
    # `final_pot_cents: None` (which every admin save and coupon mint carries
    # forward) silently rejected the freeze on every close afterwards. put_item
    # replaces the whole item, so omitting is the same as clearing, and
    # `effective()`'s DEFAULTS merge reads an absent attribute back as None.
    stored = {k: v for k, v in row.items() if v is not None}
    await run_in_thread(_table().put_item, Item=stored)
    # The STORED row, not the submitted one — what `get()` will read back.
    # Callers that need the full shape merge DEFAULTS, exactly as they do
    # for a row nobody has ever saved (`launch_settings.effective`).
    return _plain(stored)
