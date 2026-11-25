"""Spark data store, write path: rows go to Firehose, which delivers them to Iceberg tables in S3.

One JSON object per Firehose record (the Iceberg destination takes no more), sent with `PutRecordBatch` in chunks
of 500 records / 4 MiB. Nothing here ever raises into a caller and nothing blocks one: puts run in the background
(`os_voice.spawn`) on a small dedicated thread pool, and are dropped, counted, when too many are waiting. A no-op
unless `SPARK_STORE_ENABLED` and the environment is dev or prod, so local runs and CI never call AWS.

Timestamps: Firehose's Iceberg destination says "for Timestamp data types, you must always send in microseconds",
so `ts` is epoch microseconds (an int) and `dt` is the UTC day as `YYYY-MM-DD`. `bucket()` must equal the SQL
`mod(bitwise_and(from_big_endian_32(substr(sha256(to_utf8(user_id)), 1, 4)), 2147483647), 64)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import boto3
from botocore.config import Config

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

BUCKETS = 64
CHUNK_RECORDS = 500
CHUNK_BYTES = 4 * 1024 * 1024
RETRY_S = 0.2
MAX_PENDING = 200  # puts waiting or running; more are dropped and counted
# A cache hit can be up to this stale: after an opt-out in another task, a row may still be stored for up to a
# minute. The delete PR's repeat passes (45 min, 24 h) remove such rows.
CACHE_TTL_S = 60.0
READ_TIMEOUT_S = 2.0  # the opt-out read: slower than this reads False
SCRUB_MAX = 16_000  # scrub cuts its input here before any regex runs

TABLES = ("turns", "task_steps", "events")
POLL_S = 1.0  # Athena status poll
_FILTER = re.compile(r"\b(?:dt|user_bucket)\b", re.I)

_firehose = None  # created at first use; tests put a fake here
_athena = None  # likewise
# Firehose calls only: a Firehose outage can fill these 4 threads but never delays an opt-out read, which runs
# on the shared pool via run_in_thread like the other DynamoDB calls.
_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="spark-store")
_pending = 0
_cache: dict[str, tuple[float, bool]] = {}  # user_id -> (expires, allowed)
_refreshing: set[str] = set()  # people with a background refresh in flight
_forgotten: dict[str, int] = {}  # per person: how many local opt-outs; a read that started earlier is ignored


def _on() -> bool:
    return bool(settings.SPARK_STORE_ENABLED) and settings.ENVIRONMENT in ("dev", "prod")


def bucket(user_id: str | None) -> int:
    if user_id is None:
        return -1
    return (int.from_bytes(hashlib.sha256(user_id.encode()).digest()[:4], "big") & 0x7FFFFFFF) % BUCKETS


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


# --- opt-out --------------------------------------------------------------------------------------------


async def _lookup(user_id: str) -> bool:
    """Read the users row (bounded; an error or timeout reads False) and cache the answer, unless the person
    opted out locally meanwhile (`forget_locally`), which wins over a read that started before it."""
    seen = _forgotten.get(user_id, 0)
    try:
        item = (
            await asyncio.wait_for(
                run_in_thread(
                    get_table("users").get_item,
                    Key={"user_id": user_id},
                    ProjectionExpression="user_id, os_analytics_off, os_deleted_at",
                ),
                READ_TIMEOUT_S,
            )
        ).get("Item") or {}
        ok = bool(item) and not item.get("os_analytics_off") and not item.get("os_deleted_at")
    except Exception as e:  # noqa: BLE001 -- fail closed; the type only
        logger.warning("spark store opt-out read failed: %s", type(e).__name__)
        return False
    if _forgotten.get(user_id, 0) != seen:
        return False
    _remember(user_id, ok)
    return ok


async def allowed(user_id: str) -> bool:
    """May this person's data be stored: they have a row, no `os_analytics_off`, not deleted. Cached 60 s; an
    error reads False. For callers already off the request path (the event tee)."""
    if (hit := _cache.get(user_id)) and hit[0] > time.monotonic():
        return hit[1]
    return await _lookup(user_id)


def allowed_now(user_id: str) -> bool:
    """`allowed()` for the voice sideband, which must never wait on it: the cached answer when fresh; else the
    last known answer (False when there is none yet) while ONE background refresh per person runs."""
    hit = _cache.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    if user_id not in _refreshing:
        _refreshing.add(user_id)
        from core.voice.os_voice import spawn  # here: os_voice imports this module

        spawn(_refresh(user_id))
    return hit[1] if hit else False


async def _refresh(user_id: str) -> None:
    try:
        await _lookup(user_id)
    finally:
        _refreshing.discard(user_id)


def _remember(user_id: str, ok: bool) -> None:
    _cache[user_id] = (time.monotonic() + CACHE_TTL_S, ok)


def forget_locally(user_id: str) -> None:
    """The person just opted out: this process stops storing for them now, not after the cache expires."""
    _forgotten[user_id] = _forgotten.get(user_id, 0) + 1
    _remember(user_id, False)


# --- put ------------------------------------------------------------------------------------------------


def _client():
    global _firehose
    if _firehose is None:
        _firehose = boto3.client(
            "firehose",
            region_name=settings.AWS_REGION,
            config=Config(connect_timeout=2, read_timeout=5, retries={"max_attempts": 1}),
        )
    return _firehose


def _record(row: dict) -> dict:
    out = row | {"dt": datetime.fromtimestamp(row["ts"] // 1_000_000, UTC).date().isoformat()}
    out["user_bucket"] = bucket(row.get("user_id"))
    return {"Data": json.dumps(out, separators=(",", ":")).encode() + b"\n"}


def _chunks(records: list[dict]):
    chunk, size = [], 0
    for rec in records:
        n = len(rec["Data"])
        if chunk and (len(chunk) == CHUNK_RECORDS or size + n > CHUNK_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(rec)
        size += n
    if chunk:
        yield chunk


async def _send(stream: str, records: list[dict]) -> None:
    """One PutRecordBatch; the records it rejects are retried once, then counted. No text is ever logged."""
    loop = asyncio.get_running_loop()
    for attempt in (0, 1):
        code = None
        try:
            call = functools.partial(_client().put_record_batch, DeliveryStreamName=stream, Records=records)
            out = await loop.run_in_executor(_POOL, call)
            results = out.get("RequestResponses", []) if out.get("FailedPutCount") else []
            bad = [(rec, r) for rec, r in zip(records, results, strict=False) if r.get("ErrorCode")]
            code = bad[0][1]["ErrorCode"] if bad else None
            failed = [rec for rec, _ in bad]
        except Exception as e:  # noqa: BLE001
            failed = records
            code = (getattr(e, "response", None) or {}).get("Error", {}).get("Code") or type(e).__name__
        if not failed:
            return
        records = failed
        if attempt == 0:
            await asyncio.sleep(RETRY_S)
    put_metric("spark_store.put_failed", len(records))
    logger.warning("spark store put to %s failed for %d records: %s", stream, len(records), code)


def _take(n: int) -> bool:
    """Claim a pending slot; past the cap the work is dropped and counted."""
    global _pending
    if _pending >= MAX_PENDING:
        put_metric("spark_store.put_failed", n)
        return False
    _pending += 1
    return True


async def _deliver(kind: str, rows: list[dict]) -> None:
    """Send `rows` to the `kind` stream (turns, steps, events). Never raises."""
    try:
        records = [_record(row) for row in rows]
        stream = f"lightbulb-{settings.ENVIRONMENT}-spark-{kind}"
        for chunk in _chunks(records):
            await _send(stream, chunk)
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.put_failed", len(rows))
        logger.warning("spark store put failed: %s", type(e).__name__)


async def _put(kind: str, rows: list[dict]) -> None:
    global _pending
    if not _on() or not _take(len(rows)):
        return
    try:
        await _deliver(kind, rows)
    finally:
        _pending -= 1


async def put_turns(rows: list[dict]) -> None:
    await _put("turns", rows)


async def put_steps(rows: list[dict]) -> None:
    await _put("steps", rows)


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def user_of(distinct_id: str) -> str | None:
    """The user id a PostHog distinct id stands for: None for an install id (a UUID)."""
    return None if _UUID.fullmatch(distinct_id.lower()) else distinct_id


def put_event(
    distinct_id: str,
    user_id: str | None,
    install_id: str | None,
    event: str,
    props: dict,
    event_uuid: str | None = None,
) -> None:
    """Store one product event, off the caller: scheduled in the background, dropped when no loop is running."""
    if not _on():
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    from core.voice.os_voice import spawn  # here: os_voice imports this module

    row = {
        "row_id": event_uuid or str(uuid.uuid4()),
        "ts": time.time_ns() // 1000,
        "distinct_id": distinct_id,
        "user_id": user_id,
        "install_id": install_id,
        "event": event,
        "properties": json.dumps(props, default=str),
        "platform": props.get("platform"),
        "app_version": props.get("app_version"),
        "environment": props.get("environment"),
    }
    if _take(1):  # pending from here on: the opt-out read is bounded too
        spawn(_put_event(row))


async def _put_event(row: dict) -> None:
    global _pending
    try:
        if not row["user_id"] or await allowed(row["user_id"]):
            await _deliver("events", [row])
    finally:
        _pending -= 1


# --- query and forget -------------------------------------------------------------------------------------


def db() -> str:
    """The one place the database, bucket and workgroup names come from."""
    return f"lightbulb_{settings.ENVIRONMENT}_spark"


def store_bucket() -> str:
    return f"lightbulb-{settings.ENVIRONMENT}-spark-store"


def _literal(v: str | int | datetime) -> str:
    """An Athena execution-parameter value: strings must be single-quoted (quotes doubled), numbers are bare, a
    datetime is a `TIMESTAMP '...'` literal in UTC (what a `timestamp` column compares against)."""
    if isinstance(v, datetime):
        return "TIMESTAMP '" + v.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f") + "'"
    return str(int(v)) if isinstance(v, int) else "'" + v.replace("'", "''") + "'"


async def query(
    sql: str,
    params: list[str | int | datetime],
    timeout_s: float = 20,
    unfiltered: bool = False,
    workgroup: str | None = None,
) -> list[dict]:
    """Run `sql` on `workgroup` (default: the app workgroup `lightbulb-{env}-spark`) with `?` placeholders bound through ExecutionParameters, wait for it and
    return the rows as dicts (all values strings). Refuses a statement with no `dt` or `user_bucket` (a missing
    partition filter would scan the table) unless `unfiltered`, which only the retention job and VACUUM use.
    Past `timeout_s` the query is stopped and TimeoutError raised; a failed query raises RuntimeError (state only)."""
    global _athena
    if not unfiltered and not _FILTER.search(sql):
        raise ValueError("spark store query needs a dt or user_bucket filter")
    if _athena is None:
        _athena = boto3.client(
            "athena",
            region_name=settings.AWS_REGION,
            config=Config(connect_timeout=2, read_timeout=10, retries={"max_attempts": 2}),
        )
    kw = {"ExecutionParameters": [_literal(p) for p in params]} if params else {}
    started = await run_in_thread(
        _athena.start_query_execution,
        QueryString=sql,
        WorkGroup=workgroup or f"lightbulb-{settings.ENVIRONMENT}-spark",
        **kw,
    )
    qid, deadline, settled = started["QueryExecutionId"], time.monotonic() + timeout_s, False
    try:
        while True:
            status = (await run_in_thread(_athena.get_query_execution, QueryExecutionId=qid))["QueryExecution"]
            state = status["Status"]["State"]
            if state == "SUCCEEDED":
                settled = True
                break
            if state in ("FAILED", "CANCELLED"):
                settled = True
                raise RuntimeError(f"athena query {state}")  # the reason can quote SQL values: not repeated
            if time.monotonic() >= deadline:
                raise TimeoutError("athena query timed out")
            await asyncio.sleep(POLL_S)
    finally:
        if not settled:  # timed out or cancelled: do not leave it running (and billing)
            with contextlib.suppress(Exception):
                await run_in_thread(_athena.stop_query_execution, QueryExecutionId=qid)
    if not sql.lstrip().upper().startswith(("SELECT", "WITH")):
        return []  # DELETE, OPTIMIZE, VACUUM have no result set
    rows, token, header = [], None, None
    while True:
        page = await run_in_thread(
            _athena.get_query_results, QueryExecutionId=qid, **({"NextToken": token} if token else {})
        )
        for r in page["ResultSet"]["Rows"]:
            cells = [c.get("VarCharValue") for c in r["Data"]]
            if header is None:
                header = cells
            else:
                rows.append(dict(zip(header, cells, strict=False)))
        if not (token := page.get("NextToken")):
            return rows


async def forget(user_id: str, bounded: bool = True) -> None:
    """The person opted out (`bounded`: only rows up to 5 minutes after the request go) or deleted their account
    (`bounded=False`: all their rows go). Writes the job marker (so a restart cannot lose the request), then runs
    pass 1 in the background. Never raises. Gated by SPARK_STORE_DELETES, not SPARK_STORE_ENABLED: data written
    before a rollback must still be deletable. If the marker cannot be written, pass 1 still runs now and the
    marker write is retried in the background (a minute apart, for an hour). Passes 2 and 3 are the runner's."""
    if settings.ENVIRONMENT not in ("dev", "prod"):
        return
    if not settings.SPARK_STORE_DELETES:
        if settings.SPARK_STORE_ENABLED:  # rows are being stored that this request cannot delete
            put_metric("spark_store.forget_disabled")
            logger.error("spark store forget requested while SPARK_STORE_DELETES is off: nothing was deleted")
        return
    from core.observability import spark_store_jobs as jobs  # here: the jobs module imports this one
    from core.voice.os_voice import spawn

    at = jobs._now()
    try:
        await asyncio.wait_for(jobs.request_forget(user_id, at, bounded), 5)
        spawn(jobs.step(jobs.forget_key(user_id)))
    except Exception as e:  # noqa: BLE001 -- the type only
        put_metric("spark_store.forget_start_failed")
        logger.warning("spark store forget marker failed: %s", type(e).__name__)
        spawn(jobs.bounded(jobs.forget_pass(user_id, False, at + jobs.CUTOFF_SLACK if bounded else None)))
        spawn(jobs.retry_marker(user_id, at, bounded))
