"""Spark data store, delete and retention jobs, run by one in-process loop (`run`) started in main.py's lifespan.

State lives in the store bucket under `jobs/`, so a restart loses nothing and several backend tasks share it:
  jobs/forget/<sha256(user_id)>.json   {user_id, requested_at, until, next_pass, not_before, attempts, claimed_at}
  jobs/retention/<YYYY-MM-DD>.json     {not_before, attempts, done, claimed_at}
`_job` claims a marker with an S3 conditional write (create: IfNoneMatch "*", update: IfMatch <etag>; the loser of
a race is silent; a claim older than 30 minutes may be re-claimed), runs the work, and finishes with another
conditional write. Forget passes: 1 at once, 2 at requested_at + 45 min, 3 (DELETE, OPTIMIZE, VACUUM) at + 24 h,
4 (VACUUM and the count) 25 h after pass 3 finished; zero rows left after pass 4 deletes the marker. A failed pass
keeps its number and backs off (10 min * 2^attempts, at most 24 h).
"Deleted": after pass 1 the rows are invisible to queries; the text is physically gone from S3 only after pass 4,
about two days after the request (VACUUM drops only snapshots older than 24 h, so pass 3's VACUUM cannot yet remove
the files the earlier snapshots reference).
An opt-out deletes only rows with ts <= `until` (requested_at + 5 min): a person who opts back in keeps their new
data. Account deletion has `until: null`: no time condition.
NOT deleted: events sent before sign-in under an install id (no free text; gone with the 90-day retention) and other
people's rows that quote this person. Nothing here logs user text; failures are counted, never raised.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability import spark_store as store
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

FIRST_TICK_S = 60
TICK_S = 600
PASS_S = 15 * 60  # one job's time, under the claim lifetime so a live claim is never stolen
PASSES_PER_TICK = 5
CLAIM_STALE = timedelta(minutes=30)
PASS_AT = {2: timedelta(minutes=45), 3: timedelta(hours=24)}  # next_pass -> offset from requested_at
PASS_FOUR_AFTER = timedelta(hours=25)  # from the end of pass 3: its own snapshots must pass VACUUM's 24 h window
CUTOFF_SLACK = timedelta(minutes=5)  # the opt-out cache is up to 60 s stale in other tasks
BACKOFF_BASE, BACKOFF_MAX, STUCK_ATTEMPTS = timedelta(minutes=10), timedelta(hours=24), 6
RETENTION_AT = time(3, 30)  # UTC
RETENTION_DAYS = 90
QUERY_S = 300
ERROR_OBJECT_MAX = 5 * 1024 * 1024
RETRY_S, RETRY_TRIES = 60, 60  # the marker write is retried for an hour
_LOST = {"PreconditionFailed", "ConditionalRequestConflict"}

_s3 = None  # created at first use; tests put a fake here


def _now() -> datetime:  # tests pin the clock
    return datetime.now(UTC)


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client(
            "s3",
            region_name=settings.AWS_REGION,
            config=Config(connect_timeout=2, read_timeout=10, retries={"max_attempts": 2}),
        )
    return _s3


def _fail(e: Exception, what: str) -> None:
    put_metric("spark_store.jobs_failed")
    logger.warning("spark store %s failed: %s", what, type(e).__name__)  # the type only: messages can quote SQL


def _on() -> bool:
    return bool(settings.SPARK_STORE_DELETES) and settings.ENVIRONMENT in ("dev", "prod")


def forget_key(user_id: str) -> str:
    return f"jobs/forget/{hashlib.sha256(user_id.encode()).hexdigest()}.json"


async def bounded(coro: Awaitable) -> None:
    async with asyncio.timeout(PASS_S):
        await coro


# --- S3 ------------------------------------------------------------------------------------------------


async def _get(key: str) -> tuple[dict, str] | None:
    try:
        out = await run_in_thread(_client().get_object, Bucket=store.store_bucket(), Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(await run_in_thread(out["Body"].read)), out["ETag"]


async def _put(key: str, body: dict, etag: str | None = None) -> str | None:
    """Create (no etag: IfNoneMatch "*") or update (IfMatch etag); the new etag, or None when another task won."""
    cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    try:
        out = await run_in_thread(
            _client().put_object, Bucket=store.store_bucket(), Key=key, Body=json.dumps(body).encode(), **cond
        )
    except ClientError as e:
        if e.response["Error"]["Code"] in _LOST:
            return None
        raise
    return out["ETag"]


async def _delete(key: str, etag: str) -> bool:
    try:
        await run_in_thread(_client().delete_object, Bucket=store.store_bucket(), Key=key, IfMatch=etag)
    except ClientError as e:
        if e.response["Error"]["Code"] in _LOST:
            return False
        raise
    return True


async def _list(prefix: str) -> list[str]:
    keys, token = [], None
    while True:
        page = await run_in_thread(
            _client().list_objects_v2,
            Bucket=store.store_bucket(),
            Prefix=prefix,
            **({"ContinuationToken": token} if token else {}),
        )
        keys += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return keys
        token = page["NextContinuationToken"]


# --- claim, run, finish: forget and retention both use this ---------------------------------------------


def _held(m: dict, now: datetime) -> bool:
    return bool(m.get("claimed_at")) and now - datetime.fromisoformat(m["claimed_at"]) < CLAIM_STALE


def _backoff(m: dict, now: datetime) -> dict:
    attempts = m["attempts"] + 1
    wait = min(BACKOFF_BASE * 2**attempts, BACKOFF_MAX)
    return m | {"attempts": attempts, "not_before": (now + wait).isoformat()}


async def _job(
    key: str,
    create: dict | None,
    due: Callable[[dict, datetime], bool],
    work: Callable[[dict], Awaitable[bool]],
    advance: Callable[[dict, bool], dict | None],
) -> None:
    """Claim `key` (creating it from `create` when missing and `create` is given), run `work(marker)` -> ok, then
    write `advance(marker, ok)` (None deletes the marker). Never raises; a lost race is silent."""
    try:
        now = _now()
        claim = now.isoformat()
        got = await _get(key)
        if got is None:
            if create is None:
                return
            mine = create | {"claimed_at": claim}
            etag = await _put(key, mine)
        else:
            m, etag = got
            if _held(m, now) or not due(m, now):
                return
            mine = m | {"claimed_at": claim}
            etag = await _put(key, mine, etag)
        if etag is None:
            return
        try:
            async with asyncio.timeout(PASS_S):  # running out of time is a failed pass: backoff, not a repeat
                ok = await work(mine)
        except Exception as e:  # noqa: BLE001 -- a TimeoutError too; a real cancellation still propagates
            _fail(e, "job")
            ok = False
        await _finish(key, mine, lambda cur: advance(cur, ok))
    except Exception as e:  # noqa: BLE001
        _fail(e, "job claim")


async def _finish(key: str, mine: dict, advance: Callable[[dict], dict | None]) -> None:
    """Write the outcome if the marker still has my claim. If the request changed meanwhile (new requested_at),
    only release the claim: the new request starts from its own state."""
    for _ in range(3):
        got = await _get(key)
        if got is None or got[0].get("claimed_at") != mine["claimed_at"]:
            return
        cur, etag = got
        new = advance(cur) if cur.get("requested_at") == mine.get("requested_at") else cur
        if new is None:
            if await _delete(key, etag):
                return
        elif await _put(key, new | {"claimed_at": None}, etag):
            return


# --- forget --------------------------------------------------------------------------------------------


async def request_forget(user_id: str, at: datetime | None = None, bounded: bool = True) -> None:
    """Write (or bump) the marker: a new request starts all three passes again. A live claim is kept, so two
    tasks never run a pass at once. An older request than the marker's own changes nothing, except that an
    unbounded one (account deletion) widens a bounded marker; never the reverse."""
    at = at or _now()
    key = forget_key(user_id)
    until = (at + CUTOFF_SLACK).isoformat() if bounded else None
    body = {
        "user_id": user_id,
        "requested_at": at.isoformat(),
        "next_pass": 1,
        "not_before": at.isoformat(),
        "attempts": 0,
    }
    for _ in range(5):
        got = await _get(key)
        if got is None:
            if await _put(key, body | {"until": until, "claimed_at": None}):
                return
            continue
        old, etag = got
        wide = until is None or old.get("until") is None  # a marker without `until` is unbounded
        if datetime.fromisoformat(old["requested_at"]) >= at:
            if until is None and old.get("until") is not None and await _put(key, old | {"until": None}, etag):
                return
            if until is not None or old.get("until") is None:
                return
        elif await _put(key, body | {"until": None if wide else until, "claimed_at": old.get("claimed_at")}, etag):
            return
    raise RuntimeError("forget marker contended")


async def retry_marker(user_id: str, at: datetime, bounded: bool = True) -> None:
    for _ in range(RETRY_TRIES):
        await asyncio.sleep(RETRY_S)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(request_forget(user_id, at, bounded), 5)
            return


async def _forget_work(m: dict) -> bool:
    n = m["next_pass"]
    until = datetime.fromisoformat(m["until"]) if m.get("until") else None
    if n == 4:  # physical deletion only: VACUUM, then the same verification
        await _maintain([], [f"VACUUM {store.db()}.{t}" for t in store.TABLES])
        left = await _count(m["user_id"], until)
    else:
        left = await forget_pass(m["user_id"], compact=n == 3, until=until)
    if n >= 3 and left:
        put_metric("spark_store.forget_remaining")
        logger.warning("spark store forget: %d rows remain after the last pass", left)
        return False
    return True


def _advance_forget(m: dict, ok: bool) -> dict | None:
    if not ok:
        failed = _backoff(m, _now())
        if failed["attempts"] >= STUCK_ATTEMPTS:
            put_metric("spark_store.forget_stuck")
        return failed
    n = m["next_pass"]
    if n == 4:
        return None
    at = _now() + PASS_FOUR_AFTER if n == 3 else datetime.fromisoformat(m["requested_at"]) + PASS_AT[n + 1]
    return m | {"next_pass": n + 1, "not_before": at.isoformat(), "attempts": 0}


def _forget_due(m: dict, now: datetime) -> bool:
    return now >= datetime.fromisoformat(m["not_before"])


async def step(key: str) -> None:
    """Run the pass this forget marker is due for, if nobody else holds it."""
    await _job(key, None, _forget_due, _forget_work, _advance_forget)


async def _q(sql: str, params=()) -> list[dict]:
    return await store.query(
        sql, list(params), timeout_s=QUERY_S, unfiltered=sql.startswith("VACUUM"), workgroup=f"lightbulb-{settings.ENVIRONMENT}-spark-jobs"
    )  # fmt: skip


async def _all(sqls: list[tuple[str, list]]) -> None:
    """Run statements on different tables at once; the first failure is raised after all have finished."""
    for r in await asyncio.gather(*(_q(sql, p) for sql, p in sqls), return_exceptions=True):
        if isinstance(r, Exception):
            raise r


async def _maintain(optimize: list[str], vacuum: list[str]) -> None:
    """OPTIMIZE every table, then VACUUM every table whether or not an OPTIMIZE failed (physical deletion must not
    depend on compaction); each failure is counted, and the run fails after the VACUUMs were tried."""
    failed = 0
    for sql in optimize + vacuum:
        try:
            await _q(sql)
        except Exception as e:  # noqa: BLE001
            _fail(e, "maintenance")
            failed += 1
    if failed:
        raise RuntimeError(f"{failed} maintenance statements failed")


def forget_sql(user_id: str, until: datetime | None) -> tuple[list[tuple[str, list]], tuple[str, list]]:
    """The three DELETEs and the one verification count, always by user_bucket; `until` bounds them to
    ts <= until (None: no time condition, for account deletion). The count matches the DELETEs row for row."""
    b, db = store.bucket(user_id), store.db()
    time_cond, time_arg = (" AND ts <= ?", [until]) if until else ("", [])
    who = {t: ("(user_id = ? OR distinct_id = ?)", 2) if t == "events" else ("user_id = ?", 1) for t in store.TABLES}
    where = {t: f"user_bucket = ? AND {who[t][0]}{time_cond}" for t in store.TABLES}
    args = {t: [b] + [user_id] * who[t][1] + time_arg for t in store.TABLES}
    deletes = [(f"DELETE FROM {db}.{t} WHERE {where[t]}", args[t]) for t in store.TABLES]
    count = (
        "SELECT sum(c) FROM ("
        + " UNION ALL ".join(f"SELECT count(*) AS c FROM {db}.{t} WHERE {where[t]}" for t in store.TABLES)
        + ")",
        [a for t in store.TABLES for a in args[t]],
    )
    return deletes, count


async def forget_pass(user_id: str, compact: bool, until: datetime | None) -> int:
    """Delete this person's rows (up to `until`, if given), sweep errors/, with `compact` rewrite and vacuum the bucket's
    files, then count what is left (returned). Raises if any statement failed."""
    await _all(forget_sql(user_id, until)[0])
    await _sweep_errors(user_id)
    if compact:
        db, b = store.db(), store.bucket(user_id)
        await _maintain(
            [f"OPTIMIZE {db}.{t} REWRITE DATA USING BIN_PACK WHERE user_bucket = {int(b)}" for t in store.TABLES],
            [f"VACUUM {db}.{t}" for t in store.TABLES],
        )
    return await _count(user_id, until)


async def _count(user_id: str, until: datetime | None) -> int:
    rows = await _q(*forget_sql(user_id, until)[1])
    return int(float(next(iter(rows[0].values())) or 0)) if rows else 0


def _mentions(body: bytes, user_id: str) -> bool:
    uid = user_id.encode()
    if uid in body:
        return True
    for raw in re.findall(rb'"rawData"\s*:\s*"([^"]+)"', body):  # Firehose error records carry the row in base64
        with contextlib.suppress(ValueError):
            if uid in base64.b64decode(raw):
                return True
    return False


async def _sweep_errors(user_id: str) -> None:
    """Firehose parks rows it could not deliver under errors/. An object that mentions this person is deleted whole
    (privacy over keeping the other rows in it). Objects over 5 MB are not read: skipped and counted."""
    gone = skipped = 0
    for key in await _list("errors/"):
        out = await run_in_thread(_client().get_object, Bucket=store.store_bucket(), Key=key)
        body = out["Body"]
        data = (
            b""
            if out.get("ContentLength", 0) > ERROR_OBJECT_MAX
            else await run_in_thread(body.read, ERROR_OBJECT_MAX + 1)
        )
        with contextlib.suppress(Exception):
            body.close()
        if len(data) > ERROR_OBJECT_MAX or out.get("ContentLength", 0) > ERROR_OBJECT_MAX:
            skipped += 1
        elif _mentions(data, user_id):
            await run_in_thread(_client().delete_object, Bucket=store.store_bucket(), Key=key)
            gone += 1
    if gone:
        put_metric("spark_store.forget_error_objects", gone)
    if skipped:
        put_metric("spark_store.forget_error_skipped", skipped)


# --- retention -----------------------------------------------------------------------------------------


async def _retention_work(_: dict) -> bool:
    db = store.db()
    await _all(
        [(f"DELETE FROM {db}.{t} WHERE dt <= current_date - interval '{RETENTION_DAYS}' day", []) for t in store.TABLES]
    )
    await _maintain(
        [f"OPTIMIZE {db}.{t} REWRITE DATA USING BIN_PACK WHERE dt >= current_date - interval '2' day" for t in store.TABLES],
        [f"VACUUM {db}.{t}" for t in store.TABLES],
    )  # fmt: skip
    oldest = _now().date() - timedelta(days=RETENTION_DAYS)
    for t in store.TABLES:
        rows = await _q(f"SELECT min(dt) FROM {db}.{t}")
        first = next(iter(rows[0].values()), None) if rows else None
        if first and date.fromisoformat(first) <= oldest:
            put_metric("spark_store.retention_late")
    return True


async def _retention(now: datetime) -> None:
    if now.time() < RETENTION_AT:
        return
    await _job(
        f"jobs/retention/{now.date().isoformat()}.json",
        {"done": False, "attempts": 0, "not_before": now.isoformat()},
        lambda m, n: not m["done"] and n >= datetime.fromisoformat(m["not_before"]),
        _retention_work,
        lambda m, ok: m | {"done": True} if ok else _backoff(m, _now()),
    )


# --- loop ----------------------------------------------------------------------------------------------


async def tick() -> None:
    """Retention first, then the due forget markers, oldest `not_before` first, at most five passes. Each job is
    time-bounded by `_job`, so a slow one cannot use up another's time."""
    if not _on():
        return
    await _retention(_now())
    try:
        now, due = _now(), []
        for key in await _list("jobs/forget/"):
            got = await _get(key)
            if got and not _held(got[0], now) and _forget_due(got[0], now):
                due.append((got[0]["not_before"], key))
        for _, key in sorted(due)[:PASSES_PER_TICK]:
            await step(key)
    except Exception as e:  # noqa: BLE001
        _fail(e, "tick")


async def run(stop: asyncio.Event) -> None:
    """Tick every 10 minutes (the first after a minute); never raises."""
    delay = FIRST_TICK_S
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            return
        delay = TICK_S
        try:
            await tick()
        except Exception as e:  # noqa: BLE001
            _fail(e, "tick")
