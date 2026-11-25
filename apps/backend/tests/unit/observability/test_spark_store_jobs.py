"""Spark store delete and retention: S3 and Athena are fakes (S3 honours IfNoneMatch / IfMatch and ETags); no AWS."""

import asyncio
import base64
import hashlib
import io
import json
import threading
from types import SimpleNamespace
from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from core.config import settings
from core.observability import spark_store, spark_store_jobs as jobs
from core.voice import os_voice

T0 = datetime(2026, 11, 26, 10, 0, tzinfo=UTC)
USER = "user_a"
BUCKET = spark_store.bucket(USER)
MARKER = f"jobs/forget/{hashlib.sha256(USER.encode()).hexdigest()}.json"


def _err(code):
    return ClientError({"Error": {"Code": code}}, "op")


class FakeS3:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.n = 0
        self.lock = threading.Lock()
        self.log: list[str] = []

    def _etag(self):
        self.n += 1
        return f'"e{self.n}"'

    def put_object(self, Bucket, Key, Body, IfNoneMatch=None, IfMatch=None, **kw):  # noqa: N803
        with self.lock:
            cur = self.objects.get(Key)
            if IfNoneMatch == "*" and cur:
                raise _err("PreconditionFailed")
            if IfMatch and (not cur or cur[1] != IfMatch):
                raise _err("PreconditionFailed")
            etag = self._etag()
            self.objects[Key] = (Body if isinstance(Body, bytes) else Body.encode(), etag)
            self.log.append(f"put {Key}")
            return {"ETag": etag}

    def get_object(self, Bucket, Key, **kw):  # noqa: N803
        with self.lock:
            if Key not in self.objects:
                raise _err("NoSuchKey")
            body, etag = self.objects[Key]
            return {"Body": io.BytesIO(body), "ETag": etag}

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


class FakeAthena:
    def __init__(self, s3=None):
        self.queries: list[tuple[str, list[str]]] = []
        self.fail = lambda sql: False
        self.remaining = 0  # what every count(*) returns
        self.hang = False
        self.stopped: list[str] = []
        self.s3 = s3

    def start_query_execution(self, QueryString, WorkGroup, ExecutionParameters=(), **kw):  # noqa: N803
        assert WorkGroup == "lightbulb-dev-spark"
        self.queries.append((QueryString, list(ExecutionParameters)))
        if self.s3:
            self.s3.log.append("athena")
        return {"QueryExecutionId": f"q{len(self.queries)}"}

    def get_query_execution(self, QueryExecutionId):  # noqa: N803
        sql = self.queries[int(QueryExecutionId[1:]) - 1][0]
        state = "RUNNING" if self.hang else "FAILED" if self.fail(sql) else "SUCCEEDED"
        return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": "x"}}}

    def get_query_results(self, QueryExecutionId, **kw):  # noqa: N803
        sql = self.queries[int(QueryExecutionId[1:]) - 1][0]
        rows = [["_col0"], [str(self.remaining) if "count(*)" in sql else "2026-11-01"]]
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
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(spark_store, "POLL_S", 0)
    s3 = FakeS3()
    athena = FakeAthena(s3)
    monkeypatch.setattr(jobs, "_s3", s3)
    monkeypatch.setattr(spark_store, "_athena", athena)
    clock = [T0]
    monkeypatch.setattr(jobs, "_now", lambda: clock[0])
    metrics = []
    for mod in (jobs, spark_store):
        monkeypatch.setattr(mod, "put_metric", lambda name, value=1.0, **kw: metrics.append(name))
    monkeypatch.setattr(jobs, "_retention_done", None)
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
    m = env.s3.marker()
    assert m["user_id"] == USER and m["passes_done"] == [1] and m["claimed_at"] is None
    deletes = env.athena.sqls("DELETE")
    assert len(deletes) == 3 and all("user_bucket = ?" in q for q in deletes)
    assert env.athena.queries[0][1] == [str(BUCKET), f"'{USER}'"]
    assert env.athena.queries[2][1] == [str(BUCKET), f"'{USER}'", f"'{USER}'"]
    assert len(env.athena.sqls("SELECT count(*)")) == 3
    assert not env.athena.sqls("OPTIMIZE")


async def test_forget_runs_even_when_the_store_is_off_and_no_ops_outside_dev_prod(env, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)
    await spark_store.forget(USER)
    await drain()
    assert MARKER in env.s3.objects
    env.s3.objects.clear()
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    await spark_store.forget(USER)
    await drain()
    assert not env.s3.objects


async def test_forget_never_raises(env, monkeypatch):
    def boom(**kw):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(env.s3, "put_object", boom)
    await spark_store.forget(USER)
    await drain()
    assert "spark_store.forget_start_failed" in env.metrics
    assert len(env.athena.sqls("DELETE")) == 3  # still deleted, just without a marker


async def test_a_second_forget_restarts_the_clock_and_keeps_passes(env):
    await spark_store.forget(USER)
    await drain()
    await advance(timedelta(minutes=50))  # pass 2
    assert env.s3.marker()["passes_done"] == [1, 2]
    env.athena.queries.clear()
    await spark_store.forget(USER)
    await drain()
    m = env.s3.marker()
    assert m["requested_at"] == env.clock[0].isoformat() and m["passes_done"] == [1, 2]
    assert len(env.athena.sqls("DELETE")) == 3  # pass 1 again for the new request


async def test_a_failed_pass_one_is_retried_by_the_tick(env):
    env.athena.fail = lambda sql: sql.startswith("DELETE")
    await spark_store.forget(USER)
    await drain()
    m = env.s3.marker()
    assert m["passes_done"] == [] and m["claimed_at"] is None
    assert "spark_store.jobs_failed" in env.metrics
    env.athena.fail = lambda sql: False
    await advance(timedelta(minutes=10))
    assert env.s3.marker()["passes_done"] == [1]


# --- schedule ----------------------------------------------------------------------------------------------


async def test_pass_two_after_45_minutes_and_pass_three_after_24_hours_then_the_marker_goes(env):
    await spark_store.forget(USER)
    await drain()
    env.athena.queries.clear()
    await advance(timedelta(minutes=44))
    assert not env.athena.queries
    await advance(timedelta(minutes=1))
    assert len(env.athena.sqls("DELETE")) == 3 and env.s3.marker()["passes_done"] == [1, 2]
    assert not env.athena.sqls("OPTIMIZE")
    env.athena.queries.clear()
    await advance(timedelta(hours=23))
    assert not env.athena.queries
    await advance(timedelta(minutes=15))
    assert len(env.athena.sqls("DELETE")) == 3
    assert [q for q, _ in env.athena.queries if q.startswith("OPTIMIZE") or q.startswith("VACUUM")] == [
        f"OPTIMIZE lightbulb_dev_spark.{t} REWRITE DATA USING BIN_PACK WHERE user_bucket = {BUCKET}"
        if k == 0
        else f"VACUUM lightbulb_dev_spark.{t}"
        for t in ("turns", "task_steps", "events")
        for k in (0, 1)
    ]
    assert MARKER not in env.s3.objects


async def test_a_marker_older_than_three_days_counts_stuck_each_tick(env):
    env.s3.objects[MARKER] = (
        json.dumps(
            {
                "user_id": USER,
                "requested_at": (T0 - timedelta(days=4)).isoformat(),
                "passes_done": [1, 2, 3],
                "claimed_at": None,
            }
        ).encode(),
        '"x"',
    )
    await jobs.tick()
    await jobs.tick()
    assert env.metrics.count("spark_store.forget_stuck") == 2


async def test_two_runners_ticking_at_once_run_a_pass_once(env):
    env.s3.objects[MARKER] = (
        json.dumps({"user_id": USER, "requested_at": T0.isoformat(), "passes_done": [], "claimed_at": None}).encode(),
        '"x"',
    )
    await asyncio.gather(jobs.tick(), jobs.tick(), jobs.tick())
    assert len(env.athena.sqls("DELETE")) == 3
    assert env.s3.marker()["passes_done"] == [1]


async def test_a_stale_claim_is_reclaimed_after_30_minutes(env):
    env.s3.objects[MARKER] = (
        json.dumps(
            {"user_id": USER, "requested_at": T0.isoformat(), "passes_done": [], "claimed_at": T0.isoformat()}
        ).encode(),
        '"x"',
    )
    await advance(timedelta(minutes=29))
    assert not env.athena.queries
    await advance(timedelta(minutes=2))
    assert len(env.athena.sqls("DELETE")) == 3


async def test_a_failed_delete_leaves_the_marker_and_counts(env):
    await spark_store.forget(USER)
    await drain()
    env.athena.fail = lambda sql: sql.startswith("DELETE FROM lightbulb_dev_spark.task_steps")
    await advance(timedelta(minutes=50))
    assert env.s3.marker()["passes_done"] == [1] and env.s3.marker()["claimed_at"] is None
    assert "spark_store.jobs_failed" in env.metrics


async def test_non_zero_verification_after_the_last_pass_keeps_the_marker(env):
    await spark_store.forget(USER)
    await drain()
    await advance(timedelta(minutes=50))
    env.athena.remaining = 2
    await advance(timedelta(hours=25))
    assert env.s3.marker()["passes_done"] == [1, 2]
    assert "spark_store.forget_remaining" in env.metrics
    env.athena.remaining = 0
    await advance(timedelta(minutes=10))
    assert MARKER not in env.s3.objects


async def test_errors_sweep_deletes_only_objects_with_the_id(env):
    raw = base64.b64encode(json.dumps({"user_id": USER}).encode()).decode()
    env.s3.objects["errors/turns/a"] = (f"x {USER} y".encode(), '"1"')
    env.s3.objects["errors/turns/b"] = (b"user_other", '"2"')
    env.s3.objects["errors/events/c"] = (json.dumps({"rawData": raw}).encode(), '"3"')
    await jobs.forget_pass(USER, compact=False)
    assert sorted(k for k in env.s3.objects if k.startswith("errors/")) == ["errors/turns/b"]
    assert "spark_store.forget_error_objects" in env.metrics


# --- retention -----------------------------------------------------------------------------------------------


async def test_retention_runs_once_a_day_not_before_0330_utc(env, monkeypatch):
    monkeypatch.setattr(jobs, "_retention", _real_retention)
    env.clock[0] = datetime(2026, 11, 26, 3, 29, tzinfo=UTC)
    await jobs.tick()
    assert not env.athena.queries
    await advance(timedelta(minutes=1))
    assert [q for q, _ in env.athena.queries if "count" not in q and "min(dt)" not in q] == [
        stmt
        for t in ("turns", "task_steps", "events")
        for stmt in (
            f"DELETE FROM lightbulb_dev_spark.{t} WHERE dt < current_date - interval '90' day",
            f"OPTIMIZE lightbulb_dev_spark.{t} REWRITE DATA USING BIN_PACK WHERE dt >= current_date - interval '2' day",
            f"VACUUM lightbulb_dev_spark.{t}",
        )
    ]
    assert len(env.athena.sqls("SELECT min(dt)")) == 3
    assert env.s3.marker("jobs/retention/2026-11-26.json")["done"] is True
    env.athena.queries.clear()
    await advance(timedelta(hours=2))
    assert not env.athena.queries
    await advance(timedelta(days=1))
    assert env.athena.queries
    assert "jobs/retention/2026-11-27.json" in env.s3.objects


async def test_retention_late_is_counted(env, monkeypatch):
    monkeypatch.setattr(jobs, "_retention", _real_retention)
    env.clock[0] = datetime(2026, 11, 26, 4, 0, tzinfo=UTC)
    await jobs.tick()  # the fake's min(dt) is 2026-11-01: fine today, late seven months on
    env.clock[0] = datetime(2027, 6, 1, 4, 0, tzinfo=UTC)
    await jobs.tick()
    assert "spark_store.retention_late" in env.metrics


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


async def test_query_passes_parameters_and_returns_dict_rows(env):
    env.athena.remaining = 7
    rows = await spark_store.query("SELECT count(*) FROM t WHERE user_bucket = ? AND user_id = ?", [3, "o'brien"])
    assert env.athena.queries[0][1] == ["3", "'o''brien'"]
    assert rows == [{"_col0": "7"}]


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
