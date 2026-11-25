"""Spark store delete and retention: S3 and Athena are fakes (S3 honours IfNoneMatch / IfMatch and ETags); no AWS."""

import asyncio
import base64
import hashlib
import io
import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from core.config import settings
from core.observability import spark_store, spark_store_jobs as jobs
from core.voice import os_voice

T0 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
USER = "user_a"
BUCKET = spark_store.bucket(USER)
MARKER = f"jobs/forget/{hashlib.sha256(USER.encode()).hexdigest()}.json"
MIN = timedelta(minutes=1)
CUT = "TIMESTAMP '2026-11-26 10:05:00.000000'"  # T0 + 5 minutes


def _err(code):
    return ClientError({"Error": {"Code": code}}, "op")


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.n = 0
        self.lock = threading.Lock()
        self.log: list[str] = []

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **kw):  # noqa: N803
        with self.lock:
            cur = self.objects.get(Key)
            if IfNoneMatch == "*" and cur:
                raise _err("PreconditionFailed")
            if IfMatch and (not cur or cur[1] != IfMatch):
                raise _err("PreconditionFailed")
            self.n += 1
            etag = f'"e{self.n}"'
            self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), etag)
            self.log.append(f"put {Key}")
            return {"ETag": etag}

    def get_object(self, Bucket, Key, **kw):  # noqa: N803
        with self.lock:
            if Key not in self.objects:
                raise _err("NoSuchKey")
            body, etag = self.objects[Key]
            return {"Body": io.BytesIO(body), "ETag": etag, "ContentLength": len(body)}

    def delete_object(self, Bucket, Key, IfMatch=None, **kw):  # noqa: N803
        with self.lock:
            cur = self.objects.get(Key)
            if IfMatch and (not cur or cur[1] != IfMatch):
                raise _err("PreconditionFailed")
            self.objects.pop(Key, None)
            self.log.append(f"delete {Key}")

    def list_objects_v2(self, Bucket, Prefix, **kw):  # noqa: N803
        with self.lock:
            return {
                "Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False,
            }

    def marker(self, key=MARKER):
        return json.loads(self.objects[key][0])

    def seed(self, user=USER, **kw):
        m = {
            "user_id": user,
            "requested_at": T0.isoformat(),
            "next_pass": 1,
            "not_before": T0.isoformat(),
            "attempts": 0,
            "claimed_at": None,
        }
        key = f"jobs/forget/{hashlib.sha256(user.encode()).hexdigest()}.json"
        self.objects[key] = (json.dumps(m | kw).encode(), '"seed"')
        return key


class FakeAthena:
    def __init__(self, s3=None):
        self.queries: list[tuple[str, list[str]]] = []
        self.workgroups: set[str] = set()
        self.fail = lambda sql: False
        self.remaining = 0  # what the count statement returns
        self.hang = False
        self.stopped: list[str] = []
        self.s3 = s3
        self.on_start = None

    def start_query_execution(self, QueryString, WorkGroup, ExecutionParameters=(), **kw):  # noqa: N803
        self.workgroups.add(WorkGroup)
        self.queries.append((QueryString, list(ExecutionParameters)))
        if self.s3:
            self.s3.log.append("athena")
        if self.on_start:
            self.on_start(QueryString)
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803
        sql = self.queries[int(QueryExecutionId[1:]) - 1][0]
        state = "RUNNING" if self.hang else "FAILED" if self.fail(sql) else "SUCCEEDED"
        return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": "x"}}}

    def get_query_results(self, QueryExecutionId, **kw):  # noqa: N803
        sql = self.queries[int(QueryExecutionId[1:]) - 1][0]
        value = str(self.remaining) if "count(*)" in sql else "2026-11-01"
        rows = [["_col0"], [value]]
        return {"ResultSet": {"Rows": [{"Data": [{"VarCharValue": v} for v in r]} for r in rows]}}

    def stop_query_execution(self, QueryExecutionId):  # noqa: N803
        self.stopped.append(QueryExecutionId)

    def sqls(self, word):
        return [q for q, _ in self.queries if q.startswith(word)]


_real_retention = jobs._retention


async def _noop(now):
    return None


env = SimpleNamespace()  # the fixture fills it; tests and helpers read the same object


@pytest.fixture(name="env")
def _env(monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)  # deletes do not need the writes
    monkeypatch.setattr(settings, "SPARK_STORE_DELETES", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(spark_store, "POLL_S", 0)
    monkeypatch.setattr(jobs, "RETRY_S", 0)
    s3 = FakeS3()
    athena = FakeAthena(s3)
    monkeypatch.setattr(jobs, "_s3", s3)
    monkeypatch.setattr(spark_store, "_athena", athena)
    clock = [T0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    metrics = []
    for mod in (jobs, spark_store):
        monkeypatch.setattr(mod, "put_metric", lambda name, value=1.0, **kw: metrics.append(name))
    monkeypatch.setattr(jobs, "_retention", _noop)  # the retention tests put the real one back
    env.s3, env.athena, env.clock, env.metrics = s3, athena, clock, metrics
    return env


async def drain():
    await asyncio.gather(*os_voice._BACKGROUND)


async def advance(delta):
    env.clock[0] += delta
    await jobs.tick()


# --- forget ----------------------------------------------------------------------------------------------


async def test_forget_writes_the_marker_then_runs_pass_one(env):
    await spark_store.forget(USER)
    await drain()
    assert env.s3.log[:3] == [f"put {MARKER}", f"put {MARKER}", "athena"]  # marker, claim, then Athena
    assert env.s3.marker() == {
        "user_id": USER,
        "requested_at": T0.isoformat(),
        "next_pass": 2,
        "not_before": (T0 + 45 * MIN).isoformat(),
        "attempts": 0,
        "claimed_at": None,
    }
    deletes = env.athena.sqls("DELETE")
    assert len(deletes) == 3 and all("user_bucket = ?" in q and "ts <= ?" in q for q in deletes)
    by_table = {q.split()[2].rsplit(".", 1)[1]: p for q, p in env.athena.queries if q.startswith("DELETE")}
    assert by_table["turns"] == [str(BUCKET), f"'{USER}'", CUT]
    assert by_table["events"] == [str(BUCKET), f"'{USER}'", f"'{USER}'", CUT]
    counts = env.athena.sqls("SELECT sum(c)")
    assert len(counts) == 1 and counts[0].count("ts <= ?") == 3
    assert env.athena.workgroups == {"lightbulb-dev-spark-jobs"}
    assert not env.athena.sqls("OPTIMIZE")


async def test_nothing_happens_unless_deletes_are_on_or_outside_dev_prod(env, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_DELETES", False)
    env.s3.seed()
    await spark_store.forget(USER)
    await jobs.tick()
    await drain()
    assert not env.athena.queries and env.s3.log == []
    monkeypatch.setattr(settings, "SPARK_STORE_DELETES", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    await spark_store.forget(USER)
    await jobs.tick()
    await drain()
    assert not env.athena.queries and env.s3.log == []


async def test_a_repeat_request_starts_all_three_passes_again(env):
    await spark_store.forget(USER)
    await drain()
    await advance(50 * MIN)
    assert env.s3.marker()["next_pass"] == 3
    env.athena.queries.clear()
    await spark_store.forget(USER)
    await drain()
    m = env.s3.marker()
    assert m["requested_at"] == env.clock[0].isoformat() and m["next_pass"] == 2 and m["attempts"] == 0
    assert len(env.athena.sqls("DELETE")) == 3
    assert m["not_before"] == (env.clock[0] + 45 * MIN).isoformat()


async def test_pass_two_after_45_minutes_and_pass_three_after_24_hours_then_the_marker_goes(env):
    await spark_store.forget(USER)
    await drain()
    env.athena.queries.clear()
    await advance(44 * MIN)
    assert not env.athena.queries
    await advance(1 * MIN)
    assert len(env.athena.sqls("DELETE")) == 3 and env.s3.marker()["next_pass"] == 3
    assert env.s3.marker()["not_before"] == (T0 + timedelta(hours=24)).isoformat()
    assert not env.athena.sqls("OPTIMIZE")
    env.athena.queries.clear()
    await advance(timedelta(hours=23))
    assert not env.athena.queries
    await advance(15 * MIN)
    assert len(env.athena.sqls("DELETE")) == 3
    maint = [q for q, _ in env.athena.queries if q.startswith(("OPTIMIZE", "VACUUM"))]
    assert maint == [f"OPTIMIZE lightbulb_dev_spark.{t} REWRITE DATA USING BIN_PACK WHERE user_bucket = {BUCKET}" for t in jobs.store.TABLES] + [
        f"VACUUM lightbulb_dev_spark.{t}" for t in jobs.store.TABLES
    ]  # fmt: skip
    assert MARKER not in env.s3.objects


async def test_a_failed_pass_backs_off_and_counts(env):
    await spark_store.forget(USER)
    await drain()
    env.athena.fail = lambda sql: sql.startswith("DELETE FROM lightbulb_dev_spark.task_steps")
    await advance(45 * MIN)
    m = env.s3.marker()
    assert m["next_pass"] == 2 and m["attempts"] == 1 and m["claimed_at"] is None
    assert m["not_before"] == (env.clock[0] + 20 * MIN).isoformat()  # 10 min * 2**1
    assert "spark_store.jobs_failed" in env.metrics
    env.athena.queries.clear()
    await advance(19 * MIN)
    assert not env.athena.queries
    env.athena.fail = lambda sql: False
    await advance(1 * MIN)
    assert env.s3.marker()["next_pass"] == 3 and env.s3.marker()["attempts"] == 0


async def test_backoff_is_capped_and_a_stuck_marker_is_counted_from_six_attempts(env):
    env.s3.seed(attempts=7)
    env.athena.fail = lambda sql: sql.startswith("DELETE")
    await jobs.tick()
    m = env.s3.marker()
    assert m["attempts"] == 8 and "spark_store.forget_stuck" in env.metrics
    assert m["not_before"] == (T0 + timedelta(hours=24)).isoformat()  # 10 min * 2**8 > 24 h


async def test_a_failed_pass_one_from_forget_is_retried_by_the_tick(env):
    env.athena.fail = lambda sql: sql.startswith("DELETE")
    await spark_store.forget(USER)
    await drain()
    assert env.s3.marker()["next_pass"] == 1 and env.s3.marker()["attempts"] == 1
    env.athena.fail = lambda sql: False
    await advance(20 * MIN)
    assert env.s3.marker()["next_pass"] == 2


async def test_two_runners_ticking_at_once_run_a_pass_once(env):
    env.s3.seed()
    await asyncio.gather(jobs.tick(), jobs.tick(), jobs.tick())
    assert len(env.athena.sqls("DELETE")) == 3
    assert env.s3.marker()["next_pass"] == 2


async def test_a_stale_claim_is_reclaimed_after_30_minutes(env):
    env.s3.seed(claimed_at=T0.isoformat())
    await advance(29 * MIN)
    assert not env.athena.queries
    await advance(2 * MIN)
    assert len(env.athena.sqls("DELETE")) == 3


async def test_a_request_during_a_pass_keeps_the_new_request_and_frees_the_claim(env):
    env.s3.seed()
    reached, release = threading.Event(), threading.Event()

    def hold(sql):  # the pass is in the middle of its DELETEs when the person asks again
        if sql.startswith("DELETE FROM lightbulb_dev_spark.events"):
            reached.set()
            release.wait(5)

    env.athena.on_start = hold
    task = asyncio.create_task(jobs.tick())
    while not reached.is_set():
        await asyncio.sleep(0.001)
    env.clock[0] += 3 * MIN
    await jobs.request_forget(USER)
    release.set()
    await task
    m = env.s3.marker()
    assert m["requested_at"] == (T0 + 3 * MIN).isoformat() and m["next_pass"] == 1 and m["claimed_at"] is None


async def test_the_oldest_markers_run_first_five_per_tick(env):
    for i in range(7):
        env.s3.seed(user=f"user_{i}", not_before=(T0 - timedelta(minutes=10 - i)).isoformat())
    await jobs.tick()
    ran = sorted(json.loads(v[0])["user_id"] for k, v in env.s3.objects.items() if json.loads(v[0])["next_pass"] == 2)
    assert ran == [f"user_{i}" for i in range(5)]


async def test_non_zero_rows_after_the_last_pass_keep_the_marker_and_back_off(env):
    env.s3.seed(next_pass=3)
    env.athena.remaining = 2
    await jobs.tick()
    m = env.s3.marker()
    assert m["next_pass"] == 3 and m["attempts"] == 1
    assert "spark_store.forget_remaining" in env.metrics
    env.athena.remaining = 0
    await advance(20 * MIN)
    assert MARKER not in env.s3.objects


async def test_vacuum_runs_for_every_table_even_when_optimize_fails(env):
    env.s3.seed(next_pass=3)
    env.athena.fail = lambda sql: sql.startswith("OPTIMIZE")
    await jobs.tick()
    assert len(env.athena.sqls("VACUUM")) == 3
    assert env.s3.marker()["attempts"] == 1 and MARKER in env.s3.objects
    assert env.metrics.count("spark_store.jobs_failed") >= 3


async def test_a_marker_that_cannot_be_written_still_deletes_and_is_retried(env, monkeypatch):
    real, calls = env.s3.put_object, []

    def flaky(**kw):
        calls.append(1)
        if len(calls) <= 3:
            raise RuntimeError("s3 down")
        return real(**kw)

    monkeypatch.setattr(env.s3, "put_object", flaky)
    await spark_store.forget(USER)
    await drain()
    assert env.metrics.count("spark_store.forget_start_failed") == 1
    assert len(env.athena.sqls("DELETE")) == 3 and env.athena.queries[0][1][-1] == CUT  # pass 1 ran at once
    m = env.s3.marker()
    assert m["requested_at"] == T0.isoformat() and m["next_pass"] == 1  # the original request time, for the tick


async def test_errors_sweep_deletes_only_objects_with_the_id_and_skips_huge_ones(env, monkeypatch):
    raw = base64.b64encode(json.dumps({"user_id": USER}).encode()).decode()
    env.s3.objects["errors/turns/a"] = (f"x {USER} y".encode(), '"1"')
    env.s3.objects["errors/turns/b"] = (b"user_other", '"2"')
    env.s3.objects["errors/events/c"] = (json.dumps({"rawData": raw}).encode(), '"3"')
    env.s3.objects["errors/events/big"] = (USER.encode() + b"0" * 100, '"4"')
    monkeypatch.setattr(jobs, "ERROR_OBJECT_MAX", 70)
    await jobs.forget_pass(USER, False, T0)
    assert sorted(k for k in env.s3.objects if k.startswith("errors/")) == ["errors/events/big", "errors/turns/b"]
    assert "spark_store.forget_error_objects" in env.metrics and "spark_store.forget_error_skipped" in env.metrics


# --- retention -----------------------------------------------------------------------------------------------


async def test_retention_runs_once_a_day_not_before_0330_utc(env, monkeypatch):
    monkeypatch.setattr(jobs, "_retention", _real_retention)
    env.clock[0] = datetime(2026, 11, 26, 3, 29, tzinfo=UTC)
    await jobs.tick()
    assert not env.athena.queries
    await advance(1 * MIN)
    run = [q for q, _ in env.athena.queries if "min(dt)" not in q]
    assert run == [f"DELETE FROM lightbulb_dev_spark.{t} WHERE dt <= current_date - interval '90' day" for t in jobs.store.TABLES] + [
        f"OPTIMIZE lightbulb_dev_spark.{t} REWRITE DATA USING BIN_PACK WHERE dt >= current_date - interval '2' day" for t in jobs.store.TABLES
    ] + [f"VACUUM lightbulb_dev_spark.{t}" for t in jobs.store.TABLES]  # fmt: skip
    assert len(env.athena.sqls("SELECT min(dt)")) == 3
    assert env.s3.marker("jobs/retention/2026-11-26.json")["done"] is True
    env.athena.queries.clear()
    await advance(timedelta(hours=2))
    assert not env.athena.queries
    await advance(timedelta(days=1))
    assert env.athena.queries and "jobs/retention/2026-11-27.json" in env.s3.objects


async def test_retention_late_is_counted(env, monkeypatch):
    monkeypatch.setattr(jobs, "_retention", _real_retention)
    env.clock[0] = datetime(2027, 6, 1, 4, 0, tzinfo=UTC)  # the fake's min(dt) is 2026-11-01
    await jobs.tick()
    assert "spark_store.retention_late" in env.metrics


async def test_a_failed_retention_backs_off_and_still_vacuums(env, monkeypatch):
    monkeypatch.setattr(jobs, "_retention", _real_retention)
    env.clock[0] = datetime(2026, 11, 26, 4, 0, tzinfo=UTC)
    env.athena.fail = lambda sql: sql.startswith("OPTIMIZE")
    await jobs.tick()
    assert len(env.athena.sqls("VACUUM")) == 3
    m = env.s3.marker("jobs/retention/2026-11-26.json")
    assert m["done"] is False and m["attempts"] == 1 and m["claimed_at"] is None
    env.athena.queries.clear()
    await advance(10 * MIN)
    assert not env.athena.queries


async def test_every_exception_in_a_tick_is_counted_not_raised(env, monkeypatch):
    monkeypatch.setattr(jobs, "_list", lambda *a: (_ for _ in ()).throw(RuntimeError("x")))
    await jobs.tick()
    assert "spark_store.jobs_failed" in env.metrics


# --- query -----------------------------------------------------------------------------------------------------


async def test_query_refuses_unfiltered_sql(env):
    with pytest.raises(ValueError):
        await spark_store.query("SELECT * FROM lightbulb_dev_spark.turns", [])
    assert not env.athena.queries
    await spark_store.query("SELECT * FROM lightbulb_dev_spark.turns", [], unfiltered=True)
    await spark_store.query("SELECT * FROM t WHERE dt = ?", ["2026-11-26"])


async def test_query_uses_the_app_workgroup_by_default_and_passes_typed_parameters(env):
    env.athena.remaining = 7
    rows = await spark_store.query(
        "SELECT count(*) FROM t WHERE user_bucket = ? AND user_id = ? AND ts <= ?", [3, "o'brien", T0]
    )
    assert env.athena.workgroups == {"lightbulb-dev-spark"}
    assert env.athena.queries[0][1] == ["3", "'o''brien'", "TIMESTAMP '2026-11-26 10:00:00.000000'"]
    assert rows == [{"_col0": "7"}]
    await spark_store.query("SELECT 1 WHERE dt = ?", ["x"], workgroup="lightbulb-dev-spark-jobs")
    assert "lightbulb-dev-spark-jobs" in env.athena.workgroups


async def test_query_stops_the_query_at_the_timeout(env):
    env.athena.hang = True
    with pytest.raises(TimeoutError):
        await spark_store.query("SELECT 1 WHERE dt = ?", ["x"], timeout_s=0.05)
    assert env.athena.stopped == ["q1"]


async def test_a_failed_query_raises_without_the_reason(env):
    env.athena.fail = lambda sql: True
    with pytest.raises(RuntimeError) as e:
        await spark_store.query("SELECT 1 WHERE dt = ?", ["x"])
    assert "FAILED" in str(e.value) and "x" != str(e.value)
