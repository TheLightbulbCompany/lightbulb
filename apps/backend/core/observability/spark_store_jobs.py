"""Spark data store, delete and retention jobs, run by one in-process loop (`run`) started in main.py's lifespan.

State lives in the store bucket under `jobs/`, so a restart loses nothing and several backend tasks share it:
  jobs/forget/<sha256(user_id)>.json   {user_id, requested_at, passes_done, claimed_at}
  jobs/retention/<YYYY-MM-DD>.json     {claimed_at, done}
A job is claimed with an S3 conditional write: create with IfNoneMatch="*", update with IfMatch=<etag>. The loser
of a race gets 412/409 and stays silent. A claim older than 30 minutes without completion may be re-claimed (the
task that held it died). Forget passes: 1 at once, 2 when the marker is 45 minutes old, 3 (compacting) at 24 hours,
then the marker is deleted; every `forget` request clears pass 1 from `passes_done` so it runs again.
Nothing here logs user text; failures are counted (`spark_store.jobs_failed`) and retried by a later tick.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import re
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
TICK_BOUND_S = 20 * 60  # under the claim lifetime, so a live claim is never stolen by the next tick
CLAIM_STALE = timedelta(minutes=30)
PASS_AFTER = {1: timedelta(0), 2: timedelta(minutes=45), 3: timedelta(hours=24)}
STUCK_AFTER = timedelta(days=3)
RETENTION_AT = time(3, 30)  # UTC
RETENTION_DAYS = 90
QUERY_S = 300
_LOST = {"PreconditionFailed", "ConditionalRequestConflict", "412", "409"}

_s3 = None  # created at first use; tests put a fake here
_retention_done: date | None = None  # today's retention is finished: skip the S3 read until tomorrow


def _now() -> datetime:
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


def _t(iso: str) -> datetime:
    return datetime.fromisoformat(iso)


def forget_key(user_id: str) -> str:
    return f"jobs/forget/{hashlib.sha256(user_id.encode()).hexdigest()}.json"


# --- S3 ------------------------------------------------------------------------------------------------


async def _get(key: str) -> tuple[dict, str] | None:
    try:
        out = await run_in_thread(_client().get_object, Bucket=store.store_bucket(), Key=key)
    except ClientError as e:
        if e.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(await run_in_thread(out["Body"].read)), out["ETag"]


async def _put(key: str, body: dict, *, etag: str | None = None) -> str | None:
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


def _claimed(m: dict, now: datetime) -> bool:
    return bool(m.get("claimed_at")) and now - _t(m["claimed_at"]) < CLAIM_STALE


# --- forget --------------------------------------------------------------------------------------------


async def request_forget(user_id: str) -> None:
    """Write (or bump) the marker: requested_at = now, pass 1 undone, the other passes and any live claim kept."""
    key, now = forget_key(user_id), _now().isoformat()
    for _ in range(5):
        got = await _get(key)
        if got is None:
            fresh = {"user_id": user_id, "requested_at": now, "passes_done": [], "claimed_at": None}
            if await _put(key, fresh):
                return
        else:
            m, etag = got
            bumped = m | {"requested_at": now, "passes_done": [p for p in m["passes_done"] if p != 1]}
            if await _put(key, bumped, etag=etag):
                return
    raise RuntimeError("forget marker contended")


def _due(m: dict, now: datetime) -> int | None:
    age = now - _t(m["requested_at"])
    return next((n for n, after in PASS_AFTER.items() if n not in m["passes_done"] and age >= after), None)


async def step(key: str) -> None:
    """Run the pass this marker is due for, if nobody else holds it. Never raises: a failure is counted and the
    claim released, so the next tick retries (not a tight loop)."""
    try:
        got = await _get(key)
        if got is None:
            return
        m, etag = got
        now = _now()
        if now - _t(m["requested_at"]) > STUCK_AFTER:
            put_metric("spark_store.forget_stuck")
        if _claimed(m, now) or (n := _due(m, now)) is None:
            return
        claim = now.isoformat()
        if await _put(key, m | {"claimed_at": claim}, etag=etag) is None:
            return  # another task claimed it first
        try:
            remaining = await forget_pass(m["user_id"], compact=n == 3)
        except Exception:
            await _finish(key, claim, m["requested_at"], None)
            raise
        if n == 3 and remaining:
            put_metric("spark_store.forget_remaining")
            logger.warning("spark store forget: %d rows remain after the last pass", remaining)
        await _finish(key, claim, m["requested_at"], None if n == 3 and remaining else n)
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.jobs_failed")
        logger.warning("spark store forget step failed: %s", type(e).__name__)


async def _finish(key: str, claim: str, requested_at: str, done: int | None) -> None:
    """Release our claim, recording pass `done` (pass 3: delete the marker) unless the person asked again meanwhile."""
    for _ in range(3):
        got = await _get(key)
        if got is None or got[0].get("claimed_at") != claim:
            return
        m, etag = got
        if done and m["requested_at"] == requested_at:
            if done == 3:
                if await _delete(key, etag):
                    return
                continue
            m = m | {"passes_done": sorted({*m["passes_done"], done})}
        if await _put(key, m | {"claimed_at": None}, etag=etag):
            return


async def _q(sql: str, params=(), unfiltered: bool = False) -> list[dict]:
    return await store.query(sql, list(params), timeout_s=QUERY_S, unfiltered=unfiltered)


def deletes(user_id: str) -> list[tuple[str, list]]:
    """Pass SQL, (statement, ExecutionParameters), always filtered by user_bucket."""
    b, db = store.bucket(user_id), store.db()
    return [
        (f"DELETE FROM {db}.{t} WHERE user_bucket = ? AND "
         + ("(user_id = ? OR distinct_id = ?)" if t == "events" else "user_id = ?"),
         [b] + [user_id] * (2 if t == "events" else 1))
        for t in store.TABLES
    ]  # fmt: skip


def compaction(user_id: str) -> list[tuple[str, list]]:
    b, db = store.bucket(user_id), store.db()
    out = []
    for t in store.TABLES:
        # ponytail: an int we computed, inlined because Athena documents ExecutionParameters for SELECT/INSERT/CTAS/UNLOAD only
        out.append((f"OPTIMIZE {db}.{t} REWRITE DATA USING BIN_PACK WHERE user_bucket = {int(b)}", []))
        out.append((f"VACUUM {db}.{t}", []))  # VACUUM takes no filter
    return out


def counts(user_id: str) -> list[tuple[str, list]]:
    b, db = store.bucket(user_id), store.db()
    return [
        (f"SELECT count(*) FROM {db}.{t} WHERE user_bucket = ? AND user_id = ?", [b, user_id]) for t in store.TABLES
    ]


async def forget_pass(user_id: str, compact: bool) -> int:
    """Delete this person's rows, sweep errors/, with `compact` rewrite and vacuum the bucket's files, then count
    what is left. Returns the count."""
    for sql, params in deletes(user_id):
        await _q(sql, params)
    await _sweep_errors(user_id)
    if compact:
        for sql, params in compaction(user_id):
            await _q(sql, params, unfiltered=sql.startswith("VACUUM"))
    left = 0
    for sql, params in counts(user_id):
        rows = await _q(sql, params)
        left += int(next(iter(rows[0].values()))) if rows else 0
    return left


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
    """Firehose parks rows it could not deliver under errors/; delete every object that holds this person's id."""
    gone = 0
    for key in await _list("errors/"):
        out = await run_in_thread(_client().get_object, Bucket=store.store_bucket(), Key=key)
        if _mentions(await run_in_thread(out["Body"].read), user_id):
            await run_in_thread(_client().delete_object, Bucket=store.store_bucket(), Key=key)
            gone += 1
    if gone:
        put_metric("spark_store.forget_error_objects", gone)


# --- retention -----------------------------------------------------------------------------------------


async def retention_pass() -> None:
    db = store.db()
    for t in store.TABLES:
        await _q(f"DELETE FROM {db}.{t} WHERE dt < current_date - interval '{RETENTION_DAYS}' day")  # whole partitions
        await _q(f"OPTIMIZE {db}.{t} REWRITE DATA USING BIN_PACK WHERE dt >= current_date - interval '2' day")
        await _q(f"VACUUM {db}.{t}", unfiltered=True)
    oldest = _now().date() - timedelta(days=RETENTION_DAYS)
    for t in store.TABLES:
        rows = await _q(f"SELECT min(dt) FROM {db}.{t}", unfiltered=True)
        first = next(iter(rows[0].values()), None) if rows else None
        if first and date.fromisoformat(first) < oldest:
            put_metric("spark_store.retention_late")


async def _retention(now: datetime) -> None:
    global _retention_done
    today = now.date()
    if now.time() < RETENTION_AT or _retention_done == today:
        return
    key, claim = f"jobs/retention/{today.isoformat()}.json", now.isoformat()
    got = await _get(key)
    if got is None:
        etag = await _put(key, {"claimed_at": claim, "done": False})
    else:
        m, old = got
        if m.get("done"):
            _retention_done = today
            return
        etag = None if _claimed(m, now) else await _put(key, m | {"claimed_at": claim}, etag=old)
    if etag is None:
        return
    await retention_pass()
    await _put(key, {"claimed_at": None, "done": True}, etag=etag)
    _retention_done = today


# --- loop ----------------------------------------------------------------------------------------------


async def tick() -> None:
    if settings.ENVIRONMENT not in ("dev", "prod"):
        return
    try:
        for key in await _list("jobs/forget/"):
            await step(key)
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.jobs_failed")
        logger.warning("spark store job listing failed: %s", type(e).__name__)
    try:
        await _retention(_now())
    except Exception as e:  # noqa: BLE001
        put_metric("spark_store.jobs_failed")
        logger.warning("spark store retention failed: %s", type(e).__name__)


async def run(stop: asyncio.Event) -> None:
    """Tick every 10 minutes (the first after a minute), each tick time-bounded; never raises."""
    delay = FIRST_TICK_S
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        if stop.is_set():
            return
        delay = TICK_S
        try:
            async with asyncio.timeout(TICK_BOUND_S):
                await tick()
        except Exception as e:  # noqa: BLE001
            put_metric("spark_store.jobs_failed")
            logger.warning("spark store tick failed: %s", type(e).__name__)
