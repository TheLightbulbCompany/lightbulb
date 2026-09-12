"""Worker/queue/ingress observability: the signals that were missing while the
prod FIFO silently stalled behind a poisoned pointer."""

import asyncio
import time

import httpx
import pytest

from core.config import settings
from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker, WorkRetry

_ROOM = "11111111-1111-4111-8111-111111111111"
WS = "44444444-4444-4444-8444-444444444444"
KEY = "55555555-5555-4555-8555-555555555555"


class FakeSqs:
    def change_message_visibility(self, **kwargs):
        pass

    def delete_message(self, **kwargs):
        pass


def _capture_metrics(monkeypatch):
    metrics: list[tuple[str, dict]] = []
    monkeypatch.setattr(work, "put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    monkeypatch.setattr(work, "gauge", lambda name, value, **kwargs: metrics.append((name, {"value": value})))
    return metrics


def _dimension_sets(metrics, name):
    return [kwargs.get("dimensions") for metric_name, kwargs in metrics if metric_name == name]


async def _dispatch(monkeypatch, *, answer, event_kind, metrics, ledger, posted):
    """Drive one ``_dispatch_message`` against a fake container + plugin."""
    ledger.setdefault("created", [])
    ledger.setdefault("ended", [])

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return {"gateway_token": "gw"}, "10.0.0.9"

    async def fake_create_run(owner_id, run_id, **fields):
        ledger["created"].append((owner_id, run_id, fields))
        return True

    async def fake_end_run(owner_id, run_id, **fields):
        ledger["ended"].append((owner_id, run_id, fields))
        return {"runId": run_id}

    async def fake_post(*args):
        posted["body"] = args[1]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fake_get_member(*_args):
        return {"kind": "agent", "id": "a1"}

    async def fake_list_members(_room_id):
        return []

    async def fake_body(**_kwargs):
        return {"version": 1}

    monkeypatch.setattr(work, "get_ecs_manager", lambda: FakeManager())
    monkeypatch.setattr(work, "get_gateway_pool", lambda: FakePool())
    monkeypatch.setattr(work.messaging_repo, "get_member", fake_get_member)
    monkeypatch.setattr(work.messaging_repo, "list_members", fake_list_members)
    monkeypatch.setattr(work, "build_ingress_body", fake_body)
    monkeypatch.setattr(work.messaging_repo, "create_run", fake_create_run)
    monkeypatch.setattr(work.messaging_repo, "end_run", fake_end_run)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_post", fake_post)
    await worker._dispatch_message(
        {
            "ownerId": "owner-1",
            "roomId": _ROOM,
            "kind": "channel",
            "revision": 1,
            "workspaceId": WS,
            "deliveryKey": KEY,
        },
        {"kind": "agent", "id": "a1"},
        {"messageId": "m1", "messageSeq": 1, "author": {"kind": "human", "id": "u1"}},
        event_kind,
    )


class _Answer:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


@pytest.mark.asyncio
async def test_poll_failures_back_off_while_the_liveness_gauge_keeps_beating(monkeypatch):
    """A failing receive returns instantly; without a backoff the worker spins a
    hot loop, and without the gauge a dead worker is indistinguishable from an
    idle queue."""
    monkeypatch.setattr(settings, "OPENCLAW_WORK_QUEUE_URL", "https://sqs.test/q.fifo")
    metrics = _capture_metrics(monkeypatch)
    receives: list[dict] = []
    waits: list[float] = []

    class FailingSqs:
        def receive_message(self, **kwargs):
            receives.append(kwargs)
            if len(receives) <= 3:
                raise RuntimeError("throttled")
            worker.request_stop()
            return {}

    worker = OpenClawWorkWorker(sqs_client=FailingSqs())

    async def fake_wait_for(awaitable, timeout):
        waits.append(timeout)
        awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(work.asyncio, "wait_for", fake_wait_for)

    await worker.run()

    assert waits == [2, 4, 8]
    assert [name for name, _ in metrics] == [
        "messaging.worker.alive",
        "messaging.worker.poll",
        "messaging.worker.alive",
        "messaging.worker.poll",
        "messaging.worker.alive",
        "messaging.worker.poll",
        "messaging.worker.alive",
    ]
    # SentTimestamp is not returned unless it is asked for.
    assert receives[0]["AttributeNames"] == ["MessageGroupId", "SentTimestamp"]


@pytest.mark.asyncio
async def test_shutdown_during_backoff_is_not_slept_through(monkeypatch):
    """The backoff can reach 30s and main.py's graceful drain is ~20s, so the
    wait has to end on the stop signal rather than on the clock."""
    monkeypatch.setattr(settings, "OPENCLAW_WORK_QUEUE_URL", "https://sqs.test/q.fifo")
    _capture_metrics(monkeypatch)
    receives: list[dict] = []

    class FailingSqs:
        def receive_message(self, **kwargs):
            receives.append(kwargs)
            # Shutdown arrives while the queue is unreachable.
            worker.request_stop()
            raise RuntimeError("throttled")

    worker = OpenClawWorkWorker(sqs_client=FailingSqs())
    started = time.perf_counter()

    await asyncio.wait_for(worker.run(), 2)

    assert len(receives) == 1
    assert time.perf_counter() - started < 1


@pytest.mark.asyncio
async def test_queue_wait_carries_the_real_pointer_kind(monkeypatch):
    metrics = _capture_metrics(monkeypatch)
    monkeypatch.setattr(work, "deserialize_pointer", lambda _body: {"kind": "room_dispatch"})
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())

    async def waking(_pointer):
        raise WorkRetry("runtime is waking")

    monkeypatch.setattr(worker, "_room_dispatch", waking)
    sent_ms = int(time.time() * 1000) - 5_000

    await worker._process_message(
        {"Body": "{}", "ReceiptHandle": "rh-1", "Attributes": {"SentTimestamp": str(sent_ms)}}
    )

    waits = [kwargs for name, kwargs in metrics if name == "messaging.work.queue_wait"]
    assert len(waits) == 1
    assert waits[0]["unit"] == "Milliseconds"
    assert waits[0]["dimensions"] == {"kind": "room_dispatch"}
    assert 4_000 <= waits[0]["value"] <= 60_000
    # The retry metric used to hard-code "unknown", so every FIFO stall looked
    # like an undeserializable pointer.
    assert _dimension_sets(metrics, "messaging.worker.message") == [
        {"kind": "room_dispatch", "outcome": "retry", "error": "WorkRetry"}
    ]


@pytest.mark.asyncio
async def test_undeserializable_pointer_still_reports_wait_and_unknown_kind(monkeypatch):
    metrics = _capture_metrics(monkeypatch)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    sent_ms = int(time.time() * 1000) - 1_000

    await worker._process_message(
        {"Body": "not-a-stream-record", "ReceiptHandle": "rh-2", "Attributes": {"SentTimestamp": str(sent_ms)}}
    )

    assert _dimension_sets(metrics, "messaging.work.queue_wait") == [{"kind": "unknown"}]
    assert _dimension_sets(metrics, "messaging.worker.message") == [
        {"kind": "unknown", "outcome": "retry", "error": "ValueError"}
    ]


@pytest.mark.asyncio
async def test_queue_wait_is_skipped_when_sqs_omits_the_attribute(monkeypatch):
    metrics = _capture_metrics(monkeypatch)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())

    await worker._process_message({"Body": "{}", "ReceiptHandle": "rh-3"})

    assert _dimension_sets(metrics, "messaging.work.queue_wait") == []


@pytest.mark.asyncio
@pytest.mark.parametrize("event_kind", ["room_event", "user_request"])
async def test_plugin_refusal_is_counted_for_both_event_kinds(monkeypatch, event_kind):
    """A 409 on a user_request is a dropped human message; before the event_kind
    dimension it was not counted at all."""
    metrics = _capture_metrics(monkeypatch)
    ledger: dict = {}

    await _dispatch(monkeypatch, answer=_Answer(409), event_kind=event_kind, metrics=metrics, ledger=ledger, posted={})

    assert [(name, kwargs.get("dimensions")) for name, kwargs in metrics] == [
        ("messaging.run.dispatched", {"outcome": "new", "event_kind": event_kind}),
        ("messaging.ingress.post", {"status_class": "409", "event_kind": event_kind}),
        ("messaging.ingress.latency", {"event_kind": event_kind}),
        ("messaging.ambient_gate", {"outcome": "not_admitted", "event_kind": event_kind}),
    ]
    assert ledger["ended"][0][2] == {"outcome": "not_admitted"}


@pytest.mark.asyncio
async def test_accepted_ingress_reports_status_class_latency_and_outcome(monkeypatch):
    metrics = _capture_metrics(monkeypatch)

    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {"status": "accepted"}),
        event_kind="user_request",
        metrics=metrics,
        ledger={},
        posted={},
    )

    assert _dimension_sets(metrics, "messaging.ingress.post") == [{"status_class": "202", "event_kind": "user_request"}]
    assert _dimension_sets(metrics, "messaging.ingress.ack") == [{"status": "accepted", "event_kind": "user_request"}]
    latency = [kwargs for name, kwargs in metrics if name == "messaging.ingress.latency"]
    assert latency[0]["unit"] == "Milliseconds"
    assert latency[0]["value"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "status_class"), [(500, "5xx"), (400, "4xx"), (201, "other")])
async def test_plugin_error_status_is_classified(monkeypatch, status_code, status_class):
    metrics = _capture_metrics(monkeypatch)

    with pytest.raises(WorkRetry, match="plugin refused ingress"):
        await _dispatch(
            monkeypatch,
            answer=_Answer(status_code),
            event_kind="room_event",
            metrics=metrics,
            ledger={},
            posted={},
        )

    assert _dimension_sets(metrics, "messaging.ingress.post") == [
        {"status_class": status_class, "event_kind": "room_event"}
    ]
    # Latency measures the plugin answering, which it did.
    assert len([name for name, _ in metrics if name == "messaging.ingress.latency"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_class"),
    [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "transport")],
)
async def test_no_answer_is_counted_without_a_latency_sample(monkeypatch, error, status_class):
    """A timeout measures our own deadline, not the plugin's service time —
    recording it would drag the latency distribution toward the timeout value."""
    metrics = _capture_metrics(monkeypatch)

    with pytest.raises(WorkRetry, match="plugin ingress"):
        await _dispatch(monkeypatch, answer=error, event_kind="user_request", metrics=metrics, ledger={}, posted={})

    assert _dimension_sets(metrics, "messaging.ingress.post") == [
        {"status_class": status_class, "event_kind": "user_request"}
    ]
    assert _dimension_sets(metrics, "messaging.ingress.latency") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wake_result", "expected"),
    [
        ("dispatched", "dispatched"),
        ("operator_stopped", "operator_stopped"),
        # A benign race with another waker is a real outcome, not "unknown".
        ("already_running", "already_running"),
        ("in_flight", "in_flight"),
        ("converting", "converting"),
        # Only a value container_wake.py does not return folds into unknown.
        ("something_new", "unknown"),
    ],
)
async def test_wake_outcome_is_recorded_when_the_runtime_is_down(monkeypatch, wake_result, expected):
    metrics = _capture_metrics(monkeypatch)
    seen: list[str] = []

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return None, None

    async def fake_wake(owner_id):
        seen.append(owner_id)
        return wake_result

    monkeypatch.setattr(work, "get_ecs_manager", lambda: FakeManager())
    monkeypatch.setattr("core.services.container_wake.request_wake_silent", fake_wake)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())

    with pytest.raises(WorkRetry, match="runtime is waking"):
        await worker._dispatch_message(
            {"ownerId": "owner-1", "roomId": _ROOM, "kind": "channel", "revision": 1},
            {"kind": "agent", "id": "a1"},
            {"messageId": "m1", "messageSeq": 1, "author": {"kind": "human", "id": "u1"}},
            "user_request",
        )

    assert seen == ["owner-1"]
    assert _dimension_sets(metrics, "messaging.wake.outcome") == [{"outcome": expected}]


@pytest.mark.asyncio
async def test_dispatch_writes_the_ledger_row_and_the_run_contract(monkeypatch):
    metrics, ledger, posted = _capture_metrics(monkeypatch), {}, {}
    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {"status": "accepted"}),
        event_kind="user_request",
        metrics=metrics,
        ledger=ledger,
        posted=posted,
    )
    expected = work.messaging_repo.run_id_for("a1", "m1")
    assert ledger["created"][0][1] == expected
    assert posted["body"]["run"] == {
        "runId": expected,
        "workspaceId": WS,
        "frameChannel": f"/ws/{WS}/rooms/{_ROOM}/{KEY}",
        "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
    }
    assert ledger["ended"] == []
    assert {"status": "accepted", "event_kind": "user_request"} in _dimension_sets(metrics, "messaging.ingress.ack")


@pytest.mark.asyncio
async def test_replayed_and_not_admitted_are_consumed_without_retry(monkeypatch):
    metrics, ledger = _capture_metrics(monkeypatch), {}
    await _dispatch(
        monkeypatch,
        answer=_Answer(200, {"status": "replayed"}),
        event_kind="user_request",
        metrics=metrics,
        ledger=ledger,
        posted={},
    )
    assert ledger["ended"] == []
    await _dispatch(
        monkeypatch,
        answer=_Answer(409, {"error": "not_admitted"}),
        event_kind="room_event",
        metrics=metrics,
        ledger=ledger,
        posted={},
    )
    assert ledger["ended"][0][2] == {"outcome": "not_admitted"}


@pytest.mark.asyncio
async def test_a_400_from_an_unrolled_container_is_a_retry(monkeypatch):
    with pytest.raises(WorkRetry):
        await _dispatch(
            monkeypatch,
            answer=_Answer(400, {"error": "invalid_ingress"}),
            event_kind="user_request",
            metrics=_capture_metrics(monkeypatch),
            ledger={},
            posted={},
        )


def test_no_visibility_extension_task_exists():
    assert not hasattr(OpenClawWorkWorker, "_extend_visibility")
    assert work._VISIBILITY_SECONDS == 60
