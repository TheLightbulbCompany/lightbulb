"""Where a new workspace came from (T12): the self-reported answer to "How did you first
hear about us?" plus the browser's stored first- and last-touch (UTM, referrer, landing
path, ``/s`` included).

The person has no workspace yet when they answer, so the page stashes it under their user
id BEFORE creating the Clerk org (``POST /company/source``, the slug-hold pattern). The
``organization.created`` webhook copies it onto the workspace and stamps it on
``workspace_created``; ``trial_started`` reads the same record.

The door that led there rides along as ``source`` (``sign_up``, or ``session_link`` from
the session page's "Start your own workspace", T16);
the answer list is the setup form's (T22, ``schemas.setup_request.SOURCES``).

Records live in the webhook-dedup table: the user stash expires in a day, the workspace
copy is kept (no TTL). Best-effort: a missing record never blocks anything.
"""

from __future__ import annotations

import logging
import time

from core.dynamodb import run_in_thread
from schemas.setup_request import SOURCES
from core.services import webhook_dedup

logger = logging.getLogger(__name__)

#: "How did you first hear about us?" -- the one list, the setup form's (T22).
SOURCE_ANSWERS = frozenset(SOURCES)
#: Doors that bring a person to "Create your workspace" and say so themselves.
DOORS = frozenset({"sign_up", "session_link"})
TOUCH_KEYS = (
    "timestamp",
    "landing_path",
    "referrer",
    "referring_domain",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
    "ttclid",
    "li_fat_id",
)
_MAX_VALUE = 512
_MAX_DETAIL = 500
_USER_TTL_S = 86400


def clean_answer(answer: str | None, detail: str | None) -> tuple[str | None, str | None]:
    answer = answer if answer in SOURCE_ANSWERS else None
    detail = " ".join((detail or "").split())[:_MAX_DETAIL] or None
    return answer, detail if answer else None


def clean_touch(touch: object) -> dict[str, str]:
    """Only the keys the browser's attribution record has, as short strings."""
    if not isinstance(touch, dict):
        return {}
    return {k: str(v)[:_MAX_VALUE] for k in TOUCH_KEYS if isinstance((v := touch.get(k)), (str, int)) and str(v)}


def _record(answer, detail, first_touch, last_touch, door) -> dict:
    answer, detail = clean_answer(answer, detail)
    return {
        "self_reported_source": answer,
        "self_reported_detail": detail,
        "first_touch": clean_touch(first_touch),
        "last_touch": clean_touch(last_touch),
        "door": door if door in DOORS else "sign_up",
    }


async def stash_for_user(
    user_id: str,
    *,
    answer: str | None = None,
    detail: str | None = None,
    first_touch: object = None,
    last_touch: object = None,
    door: str = "sign_up",
) -> None:
    now = int(time.time())
    await run_in_thread(
        webhook_dedup._table().put_item,
        Item={
            "event_id": f"ws-source:{user_id}",
            "source": "workspace_source",
            "record": _record(answer, detail, first_touch, last_touch, door),
            "recorded_at": now,
            "ttl": now + _USER_TTL_S,
        },
    )


def as_properties(record: dict | None) -> dict:
    """Flat event properties: ``source`` (the door), ``self_reported_source``, ``first_touch_utm_source``..."""
    if not record:
        return {}
    props: dict = {k: record.get(k) for k in ("self_reported_source", "self_reported_detail")}
    props["source"] = record.get("door")
    for prefix in ("first_touch", "last_touch"):
        for key, value in (record.get(prefix) or {}).items():
            props[f"{prefix}_{key}"] = value
    return {k: v for k, v in props.items() if v}


async def adopt_for_workspace(org_id: str, user_id: str) -> dict:
    """Copy the creator's stash onto the workspace (kept) and return it as properties.
    Idempotent: a workspace that already has a record keeps it. Never raises."""
    try:
        existing = await webhook_dedup._read_event(f"ws-source:{org_id}")
        if existing.get("record"):
            return as_properties(existing["record"])
        stash = await webhook_dedup._read_event(f"ws-source:{user_id}") if user_id else {}
        record = stash.get("record") if stash and int(stash.get("ttl") or 0) > time.time() else None
        if record is None:
            return {}
        await run_in_thread(
            webhook_dedup._table().put_item,
            Item={
                "event_id": f"ws-source:{org_id}",
                "source": "workspace_source",
                "record": record,
                "recorded_at": int(time.time()),
            },
        )
        return as_properties(record)
    except Exception:
        logger.warning("workspace_source: adopt failed org=%s", org_id, exc_info=True)
        return {}
