"""Worker/queue/ingress observability: the signals that were missing while the
prod FIFO silently stalled behind a poisoned pointer."""

import time

import httpx
import pytest

from core.config import settings
from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker, WorkRetry

_ROOM = "11111111-1111-4111-8111-111111111111"
_SESSION_KEY = f"agent:a1:lightbulb:channel:{_ROOM}"


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


async def _dispatch(monkeypatch, *, answer, event_kind, metrics, cleared):
    """Drive one ``_dispatch_message`` against a fake container + plugin."""

    class FakePool:
        async def ensure_connection(self, *_args):
            return None

        async def subscribe_room_session(self, *_args):
            return None

        def mark_run_active(self, *_args, **_kwargs):
            return None

        def clear_active_run(self, *args, **kwargs):
            cleared.append((args, kwargs))

    class FakeManager:
        async def resolve_running_container(self, _owner):
            return {"gateway_token": "gw"}, "10.0.0.9"

    async def fake_post(*_args):
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
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_post", fake_post)
    await worker._dispatch_message(
        {"ownerId": "owner-1", "roomId": _ROOM, "kind": "channel", "revision": 1},
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
    sleeps: list[float] = []

    class FailingSqs:
        def receive_message(self, **kwargs):
            receives.append(kwargs)
            if len(receives) <= 3:
                raise RuntimeError("throttled")
            worker.request_stop()
            return {}

    worker = OpenClawWorkWorker(sqs_client=FailingSqs())

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(work.asyncio, "sleep", fake_sleep)

    await worker.run()

    assert sleeps == [2, 4, 8]
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
    cleared: list = []

    await _dispatch(monkeypatch, answer=_Answer(409), event_kind=event_kind, metrics=metrics, cleared=cleared)

    assert [(name, kwargs.get("dimensions")) for name, kwargs in metrics] == [
        ("messaging.ingress.post", {"status_class": "409", "event_kind": event_kind}),
        ("messaging.ingress.latency", {"event_kind": event_kind}),
        ("messaging.ambient_gate", {"outcome": "not_admitted", "event_kind": event_kind}),
    ]
    assert len(cleared) == 1


@pytest.mark.asyncio
async def test_accepted_ingress_reports_status_class_latency_and_outcome(monkeypatch):
    metrics = _capture_metrics(monkeypatch)

    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {"status": "dispatched"}),
        event_kind="user_request",
        metrics=metrics,
        cleared=[],
    )

    assert _dimension_sets(metrics, "messaging.ingress.post") == [{"status_class": "202", "event_kind": "user_request"}]
    assert _dimension_sets(metrics, "messaging.ambient_gate") == [
        {"outcome": "dispatched", "event_kind": "user_request"}
    ]
    latency = [kwargs for name, kwargs in metrics if name == "messaging.ingress.latency"]
    assert latency[0]["unit"] == "Milliseconds"
    assert latency[0]["value"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(("status_code", "status_class"), [(500, "5xx"), (400, "4xx")])
async def test_plugin_error_status_is_classified(monkeypatch, status_code, status_class):
    metrics = _capture_metrics(monkeypatch)

    with pytest.raises(WorkRetry, match="plugin refused ingress"):
        await _dispatch(
            monkeypatch,
            answer=_Answer(status_code),
            event_kind="room_event",
            metrics=metrics,
            cleared=[],
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
        await _dispatch(monkeypatch, answer=error, event_kind="user_request", metrics=metrics, cleared=[])

    assert _dimension_sets(metrics, "messaging.ingress.post") == [
        {"status_class": status_class, "event_kind": "user_request"}
    ]
    assert _dimension_sets(metrics, "messaging.ingress.latency") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wake_result", "expected"),
    [("dispatched", "dispatched"), ("operator_stopped", "operator_stopped"), ("already_running", "unknown")],
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
