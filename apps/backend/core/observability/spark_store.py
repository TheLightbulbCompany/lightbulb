"""Spark data store, write path: plain gzip JSON-lines objects in S3.

One small object per write, at `data/<table>/<user_id>/<UTC day>/<name>.json.gz`. Nothing here ever raises into
a caller and nothing blocks the event loop. A no-op unless `SPARK_STORE_ENABLED` and the environment is dev or prod,
so local runs and CI never call AWS. Expiry is an S3 lifecycle rule (CDK); deleting one person's data is `forget`.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

SCRUB_MAX = 16_000  # scrub cuts its input here before any regex runs
# `traces` is the layout the next release writes (`data/traces/<user_id>/<day>/<name>`); sweeping it first means a
# delete handled by this release also covers objects a newer task writes during a rolling deploy.
TABLES = ("turns", "task_steps", "traces")
MAX_PENDING = 200  # writes queued or running; more are dropped and counted
STALE_S = 60  # a write whose opt-out check is older than this when it runs is dropped
SECOND_SWEEP_S = 120  # `forget` runs again after this, to catch a write that was in flight
_NAME = re.compile(r"[A-Za-z0-9_-]{1,128}")

_s3 = None  # created at first use; tests reset it


def _on() -> bool:
    return bool(settings.SPARK_STORE_ENABLED) and settings.ENVIRONMENT in ("dev", "prod")


# --- scrub ----------------------------------------------------------------------------------------------


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"\D", "", m[0])
    return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m[0]


# (pattern, replacement: a template or a function). Order matters: secrets, then cards, codes, passwords.
SHAPES: list[tuple[re.Pattern, object]] = [
    (re.compile(p), "[redacted]")
    for p in (
        # bounded so a pile of BEGIN markers cannot go quadratic; a BEGIN with no END in the window is masked through it
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:[\s\S]{0,8000}?-----END [A-Z ]*PRIVATE KEY-----|[\s\S]{0,8000})",
        r"\bsk-[A-Za-z0-9_-]{16,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"\bAIza[0-9A-Za-z_-]{35}",
        r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]{5,}",
        r"\bglpat-[\w-]{20,}",
        r"\bnpm_[A-Za-z0-9]{36}",
        r"\bpypi-[A-Za-z0-9_-]{20,}",
        r"\bhf_[A-Za-z0-9]{30,}",
        r"\b[sr]k_live_[A-Za-z0-9]{16,}",
    )
] + [
    (re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _card),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[ssn]"),
    (  # the digits follow the keyword directly; "zip code 94110" and "pin it at 1500" are not codes
        re.compile(
            r"(?i)(?<!zip )(?<!area )(?<!postal )(?<!country )(\b(?:code|otp|passcode|verification|2fa|pin)\b\s*"
            r"(?:(?:is|was|:|=)\s*)?)\d{4,8}(?!\d)"
        ),
        r"\1[code]",
    ),
    (re.compile(r"(?i)\b(password|passwd|pwd)\s*(?:is|:|=)\s*\S+"), r"\1: [secret]"),
]


def scrub(text: str) -> str:
    """Mask secrets, card numbers, SSNs, one-time codes and labelled passwords; run on every text before it is
    stored. Best-effort: spoken or split-up secrets are not caught."""
    text = text[:SCRUB_MAX]
    for pattern, repl in SHAPES:
        text = pattern.sub(repl, text)
    return text


# --- the app's log upload: rows for `turns` and `task_steps` ---------------------------------------------

LOG_TYPED_MAX = 20
LOG_STEPS_KEEP = (150, 50)  # a task with more steps keeps its first 150 and last 50
CUT = "…[cut]"
_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABEL = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_SESSION = re.compile(r"vs_[0-9a-f]{32}")
_SINCE_2020_MS = 1_577_836_800_000
_DAY_MS = 86_400_000


def _pat(value: object, pattern: re.Pattern) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _ms(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _DAY_MS else None


def _ts(client_ms: object, now_us: int, default: int) -> int:
    """Epoch microseconds, never later than the server's clock: deletion by time must not miss a row because a
    client clock runs fast. A missing or absurd (before 2020) client time is `default`."""
    if not isinstance(client_ms, int) or isinstance(client_ms, bool) or client_ms < _SINCE_2020_MS:
        return default
    return min(client_ms * 1000, now_us)


def _fit(text: object, limit: int, keep_end: bool = False) -> str | None:
    """Cut to twice `limit` (so no regex sees more), scrub, then cut to `limit` characters in all, the marker
    included. `keep_end` keeps the last characters; its slice starts after its first whitespace (when it has one), so a secret cut
    in half at the slice point cannot survive as a fragment."""
    if not isinstance(text, str):
        return None
    if len(text) > 2 * limit:
        if keep_end:
            tail = text[-2 * limit :]
            text = tail[m.end() :] if (m := re.search(r"\s", tail)) else tail
        else:
            text = text[: 2 * limit]
    text = scrub(text)
    if len(text) <= limit:
        return text
    return CUT + text[-(limit - len(CUT)) :] if keep_end else text[: limit - len(CUT)] + CUT


def log_rows(user_id: str, install_id: str, body: dict, scope: str, now_us: int, meta: dict) -> tuple[list, list]:
    """The `turns` and `task_steps` rows of one upload (the shapes are the DDL's). Pure and CPU-bound: call it
    off the event loop. The user is the caller's, row ids start with it (another person cannot make the same
    ids), the clock is clamped, the scope is the server's (`problem`: a task that ended `done` keeps no text,
    unless `scope` is "all"), and every text is cut, scrubbed and cut again whatever the app did."""
    from core.observability import spark_events  # here: spark_events imports product_analytics

    who = {"platform": meta.get("platform"), "app_version": meta.get("app_version")}
    turns = []
    for ln in (body.get("typed") if isinstance(body.get("typed"), list) else [])[:LOG_TYPED_MAX]:
        ln = ln if isinstance(ln, dict) else {}
        text = _fit(ln.get("text"), 2000)
        if (
            not _pat(ln.get("id"), _ID)
            or not text
            or not text.strip()
            or ln.get("role", "person") not in ("person", "spark")
        ):
            continue
        turns.append(
            {
                "row_id": str(uuid.uuid5(spark_events.NS, f"{user_id}:{install_id}:{ln['id']}")),
                "ts": _ts(ln.get("at"), now_us, now_us),
                "user_id": user_id,
                "session_id": _pat(ln.get("sessionId"), _SESSION),
                "task_id": _pat(ln.get("taskId"), spark_events.TASK_ID),
                "role": ln.get("role", "person"),
                "source": "typed",
                "text": text,
            }
            | who
        )
    t = body.get("task")
    if not isinstance(t, dict) or not _pat(t.get("id"), spark_events.TASK_ID) or t.get("status") not in STATUSES:
        return turns, []
    source = t.get("source") if t.get("source") in ("voice", "typed", "automation") else "voice"
    keep = t["status"] != "done" or scope == "all"
    started = _ts(t.get("createdAt"), now_us, now_us)
    done = t.get("doneAt")
    common = {
        "user_id": user_id,
        "task_id": t["id"],
        "session_id": None if source == "automation" else _pat(t.get("sessionId"), _SESSION),
        "source": source,
        "automation_id": _pat(t.get("automationId"), _LABEL) if source == "automation" else None,
        "text_kept": keep,
    } | who

    def row(n: int, **kw) -> dict:
        blank = dict.fromkeys(("tool", "app", "input", "result", "ok", "ms", "status"))
        return (
            {"row_id": str(uuid.uuid5(spark_events.NS, f"{user_id}:{t['id']}:{n}")), "step_no": n} | blank | common | kw
        )

    span = done - t["createdAt"] if isinstance(done, int) and isinstance(t.get("createdAt"), int) else None
    rows = [
        row(
            0,
            ts=started,
            kind="task",
            input=_fit(t.get("request"), 4000, keep_end=True) if keep else None,
            result=_fit(t.get("answer"), 2000) if keep else None,
            ms=_ms(span),
            status=t["status"],
        )
    ]
    listed = t.get("steps") if isinstance(t.get("steps"), list) else []
    head, tail = LOG_STEPS_KEEP
    numbered = list(enumerate(listed, 1))  # a step that is not valid is skipped and keeps its number
    for n, st in numbered if len(numbered) <= head + tail else numbered[:head] + numbered[-tail:]:
        if not isinstance(st, dict) or st.get("kind") not in KINDS:
            continue
        text = keep and st["kind"] != "screenshot"
        rows.append(
            row(
                n,
                ts=_ts(st.get("at"), now_us, started),
                kind=st["kind"],
                tool=_pat(st.get("tool"), _LABEL),
                app=scrub(st["app"][:48]) if isinstance(st.get("app"), str) and st["app"] else None,
                input=_fit(st.get("input"), 1000) if text else None,
                result=_fit(st.get("result"), 1000) if text else None,
                ok=st["ok"] if isinstance(st.get("ok"), bool) else None,
                ms=_ms(st.get("ms")),
            )
        )
    return turns, rows


STATUSES = ("done", "failed", "cancelled")
KINDS = ("tool", "confirm", "error", "note", "screenshot")

# --- opt-out ----------------------------------------------------------------------------------------------


async def allowed(user_id: str) -> bool:
    """May this person's data be stored: they have a users row, no `os_analytics_off`, not deleted. A consistent
    read, never cached; an error reads False."""
    try:
        item = (
            await run_in_thread(
                get_table("users").get_item,
                Key={"user_id": user_id},
                ProjectionExpression="user_id, os_analytics_off, os_deleted_at",
                ConsistentRead=True,
            )
        ).get("Item") or {}
    except Exception as e:  # noqa: BLE001 -- fail closed; the type only
        logger.warning("spark store opt-out read failed: %s", type(e).__name__)
        return False
    return bool(item) and not item.get("os_analytics_off") and not item.get("os_deleted_at")


# --- write and delete ---------------------------------------------------------------------------------------

# Store calls run on this pool and nothing else does: a slow S3 can never starve the DynamoDB calls that meter
# voice and money, which share the event loop's default executor.
_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="spark-store")
_pending = 0  # writes queued or running
_sweeping: set[str] = set()  # people with a background delete in progress


def store_bucket() -> str:
    return f"lightbulb-{settings.ENVIRONMENT}-spark-store"


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}),
        )
    return _s3


def _valid(user_id: str) -> bool:
    return bool(_NAME.fullmatch(user_id))  # no `/` or `.`: the `/` after the id in a prefix isolates people


def _utc_string(us: int) -> str:
    """Epoch microseconds as a UTC `YYYY-MM-DD HH:MM:SS.mmm` string."""
    return (datetime(1970, 1, 1) + timedelta(microseconds=us)).isoformat(sep=" ", timespec="milliseconds")


def _write(table: str, user_id: str, name: str, rows: list[dict], checked_at: float) -> bool:
    """Runs in the store's thread. False: the opt-out check is too old by now, nothing was written."""
    if not (_valid(user_id) and _NAME.fullmatch(name)):
        raise ValueError("bad object name")
    if time.monotonic() - checked_at > STALE_S:
        return False
    lines = "".join(json.dumps(r | {"ts": _utc_string(r["ts"])}, separators=(",", ":")) + "\n" for r in rows)
    # Organized by person so that deleting one person's data is a single listing; analysis, when built, reads these
    # files as raw input. The date folder is the server's UTC day.
    key = f"data/{table}/{user_id}/{datetime.now(UTC):%Y-%m-%d}/{name}.json.gz"
    _client().put_object(Bucket=store_bucket(), Key=key, Body=gzip.compress(lines.encode()))
    return True


async def put(table: str, user_id: str, name: str, rows: list[dict], checked_at: float) -> None:
    """Write `rows` as one object. `checked_at` is the `time.monotonic()` of the opt-out check that allows it; a
    write that waits more than a minute is dropped. Never raises; failures and drops are counted, no text is logged.
    # ponytail: one small object per turn; batch per session if listing them ever gets slow."""
    global _pending
    if not (_on() and rows):
        return
    if _pending >= MAX_PENDING:
        put_metric("spark_store.put_dropped")
        return
    _pending += 1
    try:
        if not await asyncio.get_running_loop().run_in_executor(_POOL, _write, table, user_id, name, rows, checked_at):
            put_metric("spark_store.put_dropped")
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.put_failed")
        logger.warning("spark store put failed: %s", type(e).__name__)
    finally:
        _pending -= 1


def _sweep(user_id: str) -> None:
    """Delete everything of the person's in every table. One failing table does not stop the other; raises at the
    end if anything failed."""
    s3, failed = _client(), 0
    for table in TABLES:
        try:
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=store_bucket(), Prefix=f"data/{table}/{user_id}/"
            ):
                if keys := [{"Key": o["Key"]} for o in page.get("Contents", [])]:
                    out = s3.delete_objects(Bucket=store_bucket(), Delete={"Objects": keys, "Quiet": True})
                    failed += bool(out.get("Errors"))
        except Exception:  # noqa: BLE001
            failed += 1
    if failed:
        raise RuntimeError(f"{failed} deletes failed")


async def _run(user_id: str) -> None:
    await asyncio.get_running_loop().run_in_executor(_POOL, _sweep, user_id)


async def _run_counted(user_id: str) -> None:
    try:
        await _run(user_id)
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.forget_failed")
        logger.warning("spark store delete failed: %s", type(e).__name__)


async def _then_again(user_id: str, first: bool, recheck: bool) -> None:
    """The background part of a delete: the first sweep (when not already done), then a second two minutes later to
    catch a write that was in flight. `recheck`: skip the second when the person has turned usage data back on."""
    try:
        if first:
            await _run_counted(user_id)
        await asyncio.sleep(SECOND_SWEEP_S)
        if not (recheck and await allowed(user_id)):
            await _run_counted(user_id)
    finally:
        _sweeping.discard(user_id)


def _gate(user_id: str) -> bool:
    # Not gated by SPARK_STORE_ENABLED: data written before a rollback must still go.
    return settings.ENVIRONMENT in ("dev", "prod") and _valid(user_id)


def forget(user_id: str) -> None:
    """The person turned usage data off: delete their objects (both tables) in the background,
    now and again two minutes later. Skipped while one is running for them: its second sweep covers a later opt-out, because it re-checks `allowed` first. Never
    raises; a failure is counted (`spark_store.forget_failed`) and the app's next `analytics:false` retries it."""
    if not _gate(user_id) or user_id in _sweeping:
        return
    from core.voice.os_voice import spawn  # here: os_voice imports this module

    _sweeping.add(user_id)
    spawn(_then_again(user_id, first=True, recheck=True))


async def forget_account(user_id: str) -> None:
    """The person's account is being deleted: the first sweep is awaited and its failure raises (the caller keeps
    the row and the request is retried); the second runs in the background. Never skipped for one in progress."""
    if not _gate(user_id):
        return
    from core.voice.os_voice import spawn

    await _run(user_id)
    spawn(_then_again(user_id, first=False, recheck=False))
