"""POST /os/log: typed lines and a task's work log, from the app to the Spark store. The store is a fake, no AWS."""

import json
import threading
import time
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from core.config import settings
from core.observability import spark_events, spark_store
from main import app
from routers import os as os_router
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401

A, B = Tenant("user_a", None), Tenant("user_b", None)
INSTALL = "7d1c6a52-0f1e-4a3b-9c1d-2b3f4a5c6d7e"
TASK = "dlg_mhq2k8x1a3b4c5"
SESSION = "vs_" + "ab" * 16
NOW_MS = int(time.time() * 1000)
# Built from pieces at runtime so no line here looks like a credential to the repo's secret scanner.
CUT = "…[cut]"
KEY = "gh" + "p_" + "a1B2c3D4e5" * 4


class Store:
    def __init__(self):
        self.turns: list[dict] = []
        self.steps: list[dict] = []
        self.ok = True
        self.raises = False


@pytest.fixture
def store(monkeypatch, jwks):  # noqa: F811
    s = Store()

    async def allowed(user_id):
        return s.ok

    async def put_turns(rows):
        if s.raises:
            raise RuntimeError("firehose down")
        s.turns += rows

    async def put_steps(rows):
        if s.raises:
            raise RuntimeError("firehose down")
        s.steps += rows

    monkeypatch.setattr(spark_store, "allowed", allowed)
    monkeypatch.setattr(spark_store, "put_turns", put_turns)
    monkeypatch.setattr(spark_store, "put_steps", put_steps)
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "SPARK_WORKLOG_SCOPE", "problem")
    os_router._LOG_PER_USER.clear()
    return s


async def post(body, who=A, content=None, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        h = (who.headers if who else {}) | (headers or {})
        return await c.post("/api/v1/os/log", headers=h, json=body if content is None else None, content=content)


def payload(**kw):
    return {"installId": INSTALL, "appVersion": "2.1.18", "platform": "mac"} | kw


def line(i="ln_1", **kw):
    return {"id": i, "at": NOW_MS - 1000, "text": "open the calendar"} | kw


def step(**kw):
    return {
        "at": NOW_MS - 500,
        "kind": "tool",
        "tool": "click",
        "app": "Safari",
        "input": '{"x":1}',
        "result": "ok",
        "ok": True,
        "ms": 340,
    } | kw


def task(status="failed", steps=None, **kw):
    return {
        "id": TASK,
        "sessionId": SESSION,
        "source": "voice",
        "status": status,
        "createdAt": NOW_MS - 5000,
        "doneAt": NOW_MS - 1000,
        "request": "book a table",
        "answer": "done, booked",
        "steps": [step()] if steps is None else steps,
    } | kw


# --- gates ---------------------------------------------------------------------------------------------


async def test_not_signed_in_is_refused(store):
    r = await post(payload(typed=[line()]), who=None)
    assert r.status_code in (401, 403)
    assert not store.turns


async def test_over_512_kb_is_413(store):
    r = await post(None, content=b" " * 512_001)
    assert r.status_code == 413
    r = await post(None, content=json.dumps(payload(typed=[line(text="x" * 600_000)])).encode())
    assert r.status_code == 413


async def test_malformed_json_or_bad_install_is_400(store):
    assert (await post(None, content=b"{nope")).status_code == 400
    assert (await post({"typed": [line()]})).status_code == 400
    assert (await post(payload(installId="not-a-uuid"))).status_code == 400


async def test_an_opted_out_user_gets_204_and_nothing_is_stored(store):
    store.ok = False
    r = await post(payload(typed=[line()], task=task()))
    assert r.status_code == 204
    assert not store.turns and not store.steps


async def test_flag_off_is_204_and_nothing_is_stored(store, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)
    r = await post(payload(typed=[line()], task=task()))
    assert r.status_code == 204
    assert not store.turns and not store.steps


async def test_a_store_failure_never_reaches_the_app(store):
    store.raises = True
    assert (await post(payload(typed=[line()], task=task()))).status_code == 204


async def test_over_the_rate_limit_is_204_and_dropped(store, monkeypatch):
    monkeypatch.setattr(os_router, "_LOG_PER_USER", os_router.RateBucket(2))
    for _ in range(3):
        assert (await post(payload(typed=[line()]))).status_code == 204
    assert len(store.turns) == 2


async def test_a_normal_session_never_hits_the_limit(store):
    for i in range(10):
        assert (await post(payload(typed=[line(f"ln_{i}")]))).status_code == 204
    assert len(store.turns) == 10


async def test_config_returns_the_scope(monkeypatch):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get("/api/v1/os/config")).json()["worklogScope"] == "problem"
        monkeypatch.setattr(settings, "SPARK_WORKLOG_SCOPE", "all")
        assert (await c.get("/api/v1/os/config")).json()["worklogScope"] == "all"
        monkeypatch.setattr(settings, "SPARK_WORKLOG_SCOPE", "bogus")
        assert (await c.get("/api/v1/os/config")).json()["worklogScope"] == "problem"


# --- identity ------------------------------------------------------------------------------------------


async def test_the_user_id_comes_from_auth_not_the_body(store):
    body = payload(typed=[line()], task=task(), userId="user_b", user_id="user_b")
    body["task"]["userId"] = "user_b"
    body["typed"][0]["userId"] = "user_b"
    await post(body, who=A)
    assert {r["user_id"] for r in store.turns + store.steps} == {"user_a"}


# --- typed lines ---------------------------------------------------------------------------------------


async def test_a_typed_line_row(store):
    await post(payload(typed=[line(taskId=TASK, sessionId=SESSION), line("ln_2", role="spark", text="sure")]))
    first, second = store.turns
    assert first == {
        "row_id": str(uuid.uuid5(spark_events.NS, f"user_a:{INSTALL}:ln_1")),
        "ts": (NOW_MS - 1000) * 1000,
        "user_id": "user_a",
        "session_id": SESSION,
        "task_id": TASK,
        "role": "person",
        "source": "typed",
        "text": "open the calendar",
        "platform": "mac",
        "app_version": "2.1.18",
    }
    assert second["role"] == "spark" and second["task_id"] is None and second["session_id"] is None


async def test_bad_typed_lines_are_dropped_one_by_one(store):
    await post(
        payload(
            typed=[
                line("bad id!"),
                line("ln_e", text="   "),
                line("ln_r", role="robot"),
                "x",
                line("ln_ok", taskId="nope"),
            ]
        )
    )
    (only,) = store.turns
    assert only["task_id"] is None and only["text"] == "open the calendar"


async def test_at_most_20_typed_lines(store):
    await post(payload(typed=[line(f"ln_{i}") for i in range(30)]))
    assert len(store.turns) == 20


# --- tasks ---------------------------------------------------------------------------------------------


async def test_task_and_step_rows(store):
    await post(payload(task=task("failed", steps=[step(), step(kind="error", tool=None, app=None, ok=False, ms=None)])))
    assert store.turns == []
    head, one, two = store.steps
    assert head == {
        "row_id": str(uuid.uuid5(spark_events.NS, f"user_a:{TASK}:0")),
        "ts": (NOW_MS - 5000) * 1000,
        "user_id": "user_a",
        "task_id": TASK,
        "session_id": SESSION,
        "source": "voice",
        "automation_id": None,
        "step_no": 0,
        "kind": "task",
        "tool": None,
        "app": None,
        "input": "book a table",
        "result": "done, booked",
        "ok": None,
        "ms": 4000,
        "status": "failed",
        "text_kept": True,
        "platform": "mac",
        "app_version": "2.1.18",
    }
    assert one == {
        "row_id": str(uuid.uuid5(spark_events.NS, f"user_a:{TASK}:1")),
        "ts": (NOW_MS - 500) * 1000,
        "user_id": "user_a",
        "task_id": TASK,
        "session_id": SESSION,
        "source": "voice",
        "automation_id": None,
        "step_no": 1,
        "kind": "tool",
        "tool": "click",
        "app": "Safari",
        "input": '{"x":1}',
        "result": "ok",
        "ok": True,
        "ms": 340,
        "status": None,
        "text_kept": True,
        "platform": "mac",
        "app_version": "2.1.18",
    }
    assert two["step_no"] == 2 and two["kind"] == "error" and two["ok"] is False


async def test_an_automation_task_carries_its_id_and_no_session(store):
    await post(payload(task=task(source="automation", automationId="auto_1", sessionId=SESSION)))
    head = store.steps[0]
    assert head["source"] == "automation" and head["automation_id"] == "auto_1" and head["session_id"] is None


@pytest.mark.parametrize("bad", [{"id": "dlg_BAD"}, {"status": "weird"}, {"id": None}])
async def test_a_bad_task_is_dropped_but_typed_lines_are_kept(store, bad):
    await post(payload(typed=[line()], task=task(**bad)))
    assert len(store.turns) == 1 and store.steps == []


async def test_unknown_fields_and_screenshots_are_never_stored(store):
    s = step(
        kind="screenshot",
        result="data:image/png;base64,AAAA",
        input="/tmp/x.png",
        screenshotPath="/tmp/x.png",
        image="AAAA",
    )
    await post(payload(task=task(steps=[s]), screenshot="AAAA"))
    shot = store.steps[1]
    assert shot["kind"] == "screenshot" and shot["input"] is None and shot["result"] is None
    assert "AAAA" not in json.dumps(store.steps) and "/tmp" not in json.dumps(store.steps)


async def test_a_bad_step_is_skipped_but_keeps_its_number(store):
    await post(payload(task=task(steps=[step(), step(kind="thinking"), step()])))
    assert [r["step_no"] for r in store.steps] == [0, 1, 3]


async def test_labels_are_matched_cut_and_scrubbed(store):
    steps = [step(tool="pwd is hunter2", app="pwd is hunter2"), step(tool="click", app="Visual Studio Code " * 5)]
    await post(payload(task=task("done", steps=steps, source="automation", automationId="pwd is hunter2")))
    head, one, two = store.steps
    assert head["automation_id"] is None
    assert one["tool"] is None and one["app"] == "pwd: [secret]"
    assert two["tool"] == "click" and len(two["app"]) == 48
    assert "hunter2" not in json.dumps(store.steps)


async def test_bad_numbers_and_platform_become_null(store):
    await post(payload(task=task(steps=[step(ms=-5)]), platform="<script>", appVersion="x y"))
    row = store.steps[1]
    assert row["ms"] is None and row["platform"] is None and row["app_version"] is None


# --- scope ---------------------------------------------------------------------------------------------


async def test_a_done_task_is_category_only_under_problem(store):
    await post(payload(task=task("done")))
    for r in store.steps:
        assert r["input"] is None and r["result"] is None and r["text_kept"] is False
    assert store.steps[0]["status"] == "done" and store.steps[1]["tool"] == "click" and store.steps[1]["ms"] == 340


@pytest.mark.parametrize("status", ["failed", "cancelled"])
async def test_a_problem_task_keeps_its_text(store, status):
    await post(payload(task=task(status)))
    assert all(r["text_kept"] is True for r in store.steps)
    assert store.steps[0]["input"] == "book a table" and store.steps[1]["result"] == "ok"


async def test_scope_all_keeps_text_for_a_done_task(store, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_WORKLOG_SCOPE", "all")
    await post(payload(task=task("done")))
    assert all(r["text_kept"] is True for r in store.steps)
    assert store.steps[0]["result"] == "done, booked"


# --- scrub and limits ----------------------------------------------------------------------------------


async def test_every_text_field_is_scrubbed(store):
    t = task("failed", request=f"use {KEY}", answer=f"used {KEY}", steps=[step(input=f"k {KEY}", result=f"r {KEY}")])
    await post(payload(typed=[line(text=f"my key {KEY}")], task=t))
    assert KEY not in json.dumps(store.turns + store.steps)
    assert store.turns[0]["text"] == "my key [redacted]"
    assert store.steps[0]["input"] == "use [redacted]" and store.steps[0]["result"] == "used [redacted]"
    assert store.steps[1]["input"] == "k [redacted]" and store.steps[1]["result"] == "r [redacted]"


@pytest.mark.parametrize(
    ("where", "limit", "keep_end"),
    [
        ("typed", 2000, False),
        ("request", 4000, True),
        ("answer", 2000, False),
        ("input", 1000, False),
        ("result", 1000, False),
    ],
)
async def test_each_text_is_cut_to_its_limit(store, where, limit, keep_end):
    big = "START" + "m " * 4500 + "THE ASK"
    st = step(input=big if where == "input" else "i", result=big if where == "result" else "r")
    t = task(request=big if where == "request" else "q", answer=big if where == "answer" else "a", steps=[st])
    await post(payload(typed=[line(text=big if where == "typed" else "t")], task=t))
    rows = {"typed": store.turns[0]["text"], "request": store.steps[0]["input"], "answer": store.steps[0]["result"]}
    rows |= {"input": store.steps[1]["input"], "result": store.steps[1]["result"]}
    got = rows[where]
    assert len(got) == limit
    assert (
        (got.startswith(CUT) and got.endswith("THE ASK"))
        if keep_end
        else (got.startswith("START") and got.endswith(CUT))
    )


async def test_over_200_steps_keep_the_first_150_and_last_50(store):
    steps = [step(input=f"s{i}") for i in range(1, 251)]
    await post(payload(task=task(steps=steps)))
    nums = [r["step_no"] for r in store.steps]
    assert nums == [0, *range(1, 151), *range(201, 251)]
    assert store.steps[151]["input"] == "s201"


# --- time ----------------------------------------------------------------------------------------------


async def test_a_future_client_time_is_clamped_to_server_now(store):
    far = NOW_MS + 10 * 86_400_000
    before = time.time_ns() // 1000
    await post(payload(typed=[line(at=far)], task=task(createdAt=far, doneAt=far, steps=[step(at=far)])))
    after = time.time_ns() // 1000
    for r in store.turns + store.steps:
        assert before <= r["ts"] <= after


@pytest.mark.parametrize("at", [None, 0, -5, 12345])
async def test_a_missing_or_absurd_client_time_is_server_now(store, at):
    before = time.time_ns() // 1000
    await post(payload(typed=[line(at=at)]))
    assert before <= store.turns[0]["ts"] <= time.time_ns() // 1000


async def test_step_order_is_by_number_not_time(store):
    await post(payload(task=task(steps=[step(at=NOW_MS - 100), step(at=NOW_MS - 900)])))
    assert [r["step_no"] for r in store.steps] == [0, 1, 2]


# --- dedupe --------------------------------------------------------------------------------------------


async def test_the_same_payload_twice_gives_the_same_row_ids(store):
    body = payload(typed=[line()], task=task())
    await post(body)
    first = [r["row_id"] for r in store.turns + store.steps]
    store.turns.clear()
    store.steps.clear()
    os_router._LOG_PER_USER.clear()
    await post(body)
    assert [r["row_id"] for r in store.turns + store.steps] == first
    assert len(set(first)) == len(first) == 3


# --- review fixes --------------------------------------------------------------------------------------


async def test_row_ids_belong_to_the_caller(store):
    body = payload(typed=[line()], task=task())
    await post(body, who=A)
    mine = [r["row_id"] for r in store.turns + store.steps]
    store.turns.clear()
    store.steps.clear()
    await post(body, who=B)
    theirs = [r["row_id"] for r in store.turns + store.steps]
    assert len(theirs) == 3 and not set(mine) & set(theirs)


async def test_rows_are_built_off_the_event_loop(store, monkeypatch):
    seen = []
    real = spark_store.log_rows

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(spark_store, "log_rows", spy)
    await post(payload(typed=[line()]))
    assert seen and seen[0] != threading.get_ident()


async def test_an_opt_out_check_that_raises_is_still_204(store, monkeypatch):
    async def boom(user_id):
        raise RuntimeError("dynamo down")

    monkeypatch.setattr(spark_store, "allowed", boom)
    assert (await post(payload(typed=[line()]))).status_code == 204


async def test_a_body_of_unterminated_key_markers_is_cheap(store):
    marker = "-----BEGIN RSA PRIVATE KEY-----"
    junk = marker * 400
    steps = [step(input=junk, result=junk) for _ in range(15)]
    t0 = time.perf_counter()
    r = await post(payload(task=task(steps=steps, request=junk, answer=junk)))
    assert r.status_code == 204 and time.perf_counter() - t0 < 0.5
    assert "-----BEGIN" not in json.dumps(store.steps)
