"""Worker/queue/ingress observability: the signals that were missing while the
prod FIFO silently stalled behind a poisoned pointer."""

import time

import httpx
import pytest
from unittest.mock import AsyncMock

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
    return metrics


def _dimension_sets(metrics, name):
    return [kwargs.get("dimensions") for metric_name, kwargs in metrics if metric_name == name]


async def _dispatch(monkeypatch, *, answer, event_kind, metrics, ledger, posted, meta=None, body=None, members=None):
    """Drive one ``_dispatch_message`` against a fake container + plugin."""
    ledger.setdefault("created", [])
    ledger.setdefault("ended", [])

    async def fake_create_run(owner_id, run_id, **fields):
        ledger["created"].append((owner_id, run_id, fields))
        return True

    async def fake_end_run(owner_id, run_id, **fields):
        ledger["ended"].append((owner_id, run_id, fields))
        return {"runId": run_id}

    async def fake_post(*args):
        posted["body"] = args[2]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def fake_get_member(*_args):
        return {"kind": "agent", "id": "a1"}

    async def fake_list_members(_room_id):
        return members or []

    async def fake_body(**_kwargs):
        return {"version": 1}

    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work.messaging_repo, "get_member", fake_get_member)
    monkeypatch.setattr(work.messaging_repo, "list_members", fake_list_members)
    monkeypatch.setattr(work, "build_ingress_body", body or fake_body)
    monkeypatch.setattr(work.messaging_repo, "create_run", fake_create_run)
    monkeypatch.setattr(work.messaging_repo, "list_open_runs_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(work.messaging_repo, "end_run", fake_end_run)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())
    monkeypatch.setattr(worker, "_post", fake_post)
    await worker._dispatch_message(
        meta
        if meta is not None
        else {
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

    # Jev is unconfigured in tests, so Listening traffic records a fallback first.
    routed = [("messaging.jev.route", {"outcome": "fallback"})] if event_kind == "room_event" else []
    assert [(name, kwargs.get("dimensions")) for name, kwargs in metrics] == routed + [
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
@pytest.mark.parametrize(("status_code", "status_class"), [(500, "5xx"), (429, "4xx"), (201, "other")])
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
async def test_a_4xx_refusal_is_permanent_closed_and_visible_not_retried(monkeypatch):
    """Prod 2026-09-28: a zip in recent history 400'd every dispatch; ~30 retries went to the
    DLQ and the agent never replied again. A 4xx now closes the run with a room note."""
    from core.services import messaging

    metrics = _capture_metrics(monkeypatch)
    closed, frames, notes = [], [], []

    async def fake_end(owner_id, run_id, **fields):
        closed.append(fields)
        return {"runId": run_id, "roomId": "r", "agentId": "a"}

    async def fake_frames(row, kind):
        frames.append(kind)

    async def fake_note(owner_id, row, outcome, kind):
        notes.append((outcome, kind))

    monkeypatch.setattr(messaging, "end_run_and_announce", fake_end)
    monkeypatch.setattr(messaging, "publish_run_error_frames", fake_frames)
    monkeypatch.setattr(messaging, "post_run_failure_message", fake_note)

    await _dispatch(monkeypatch, answer=_Answer(400), event_kind="user_request", metrics=metrics, ledger={}, posted={})

    assert closed == [{"outcome": "error", "error_kind": "ingress_refused"}]
    assert frames == ["ingress_refused"] and notes == [("error", "ingress_refused")]
    assert _dimension_sets(metrics, "messaging.ingress.refused") == [{"status": "400", "event_kind": "user_request"}]


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
@pytest.mark.parametrize("row", [None, {"gateway_token": "gw"}, {"gateway_token": "gw", "agentcore_runtime_arn": ""}])
async def test_a_workspace_with_no_runtime_retries_and_records_the_outcome(monkeypatch, row):
    """There is no wake step left to measure: the ingress invoke itself starts the microVM, so
    the ONLY reason a workspace has no addressable runtime is that provisioning has not finished.
    That must RETRY (the queue's backoff is the wait), never drop the message -- and it has to be
    visible, or a whole workspace silently never answers.

    The three rows are the shapes provisioning passes through: no row at all, a row before the
    runtime is created, and a row whose field was written empty.
    """
    metrics = _capture_metrics(monkeypatch)
    monkeypatch.setattr(work.container_repo, "get_by_owner_id", AsyncMock(return_value=row))
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())

    with pytest.raises(WorkRetry, match="not provisioned"):
        await worker._dispatch_message(
            {"ownerId": "owner-1", "roomId": _ROOM, "kind": "channel", "revision": 1},
            {"kind": "agent", "id": "a1"},
            {"messageId": "m1", "messageSeq": 1, "author": {"kind": "human", "id": "u1"}},
            "user_request",
        )

    assert _dimension_sets(metrics, "messaging.wake.outcome") == [{"outcome": "no_runtime"}]


@pytest.mark.asyncio
async def test_the_ingress_post_passes_the_path_not_the_body(monkeypatch):
    """Regression guard on ``_post``'s signature. It used to take an ``ip`` between the container
    and the path; dropping that parameter without dropping it from the ONE call site shifted every
    argument by a position -- the request went out with the body dict as its PATH, the bearer
    token as its JSON, and the local-session token as its bearer. Every ingress in the fleet, and
    nothing in these tests catches it, because they all stub ``_post``."""
    seen = {}

    async def fake_request(container, path, **kwargs):
        seen.update(container=container, path=path, **kwargs)

        class _R:
            status_code = 200

            def json(self):
                return {"accepted": True}

        return _R()

    monkeypatch.setattr(work.runtime_transport, "request", fake_request)
    worker = OpenClawWorkWorker(sqs_client=FakeSqs())

    await worker._post({"agentcore_runtime_arn": "arn:r1"}, "/plugins/lightbulb-channel/inbound/a1", {"x": 1}, "tok")

    assert seen["path"] == "/plugins/lightbulb-channel/inbound/a1"
    assert seen["json_body"] == {"x": 1}
    assert seen["headers"]["Authorization"] == "Bearer tok"
    assert "X-Lightbulb-Local-Session-Token" not in seen["headers"]


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
async def test_dispatch_heals_a_meta_row_missing_workspace_id(monkeypatch):
    """A META row that predates workspaceId is healed lazily, and the run object
    that reaches the container + the ledger row both carry the healed id."""
    heal_calls = []

    async def fake_heal(pk, sk, meta):
        heal_calls.append((pk, sk, meta))
        return WS

    monkeypatch.setattr(work.messaging_repo, "workspace_id_for_meta", fake_heal)
    metrics, ledger, posted = _capture_metrics(monkeypatch), {}, {}
    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {"status": "accepted"}),
        event_kind="user_request",
        metrics=metrics,
        ledger=ledger,
        posted=posted,
        meta={"ownerId": "owner-1", "roomId": _ROOM, "kind": "channel", "revision": 1, "deliveryKey": KEY},
    )
    assert heal_calls == [(work.messaging_repo.room_pk(_ROOM), "META", heal_calls[0][2])]
    expected = work.messaging_repo.run_id_for("a1", "m1")
    assert posted["body"]["run"] == {
        "runId": expected,
        "workspaceId": WS,
        "frameChannel": f"/ws/{WS}/rooms/{_ROOM}/{KEY}",
        "frameEndpoint": settings.APPSYNC_EVENTS_HTTP_ENDPOINT,
    }
    assert ledger["created"][0][2]["workspace_id"] == WS


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
async def test_build_ingress_body_carries_sessions_only_when_given(monkeypatch):
    async def no_history(*_args, **_kwargs):
        return []

    monkeypatch.setattr(work.messaging_repo, "list_messages_before", no_history)
    monkeypatch.setattr(work, "_public_message", lambda row: dict(row))
    message = {
        "messageId": "m1",
        "messageSeq": 3,
        "author": {"kind": "human", "id": "u1", "displayName": "Founder"},
        "mentions": [],
    }
    meta = {"roomId": _ROOM, "name": "launch", "kind": "channel", "revision": 1}
    members = [{"kind": "agent", "id": "a1", "displayName": "Friday", "listenMode": "active"}]
    summary = [
        {
            "sessionId": "s1",
            "humanUserId": "u1",
            "displayName": "Founder",
            "provider": "claude",
            "cwdName": "repo",
            "title": "Fix",
            "status": "busy",
            "latestEventSeq": 7,
            "lastEventAt": "2026-09-14T00:00:00Z",
        }
    ]
    with_sessions = await work.build_ingress_body(
        meta=meta, target_agent_id="a1", event_kind="user_request", message=message, members=members, sessions=summary
    )
    assert with_sessions["sessions"] == summary
    without = await work.build_ingress_body(
        meta=meta, target_agent_id="a1", event_kind="user_request", message=message, members=members
    )
    assert "sessions" not in without


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "sent"),
    [([{"sessionId": "s1"}], [{"sessionId": "s1"}]), ([], [])],
    ids=["live-session", "no-live-session"],
)
async def test_dispatch_passes_the_live_summary_through_unchanged(monkeypatch, live, sent):
    """The dispatch call site no longer collapses an empty list to None -- that
    conditionality now lives solely in build_ingress_body's ``if sessions:``
    (covered by test_build_ingress_body_carries_sessions_only_when_given)."""
    from core.services import local_sessions

    seen: dict = {}

    async def fake_live(owner, room_id, members):
        seen["live"] = (owner, room_id)
        return live

    async def fake_body(**kwargs):
        seen["sessions"] = kwargs.get("sessions")
        return {"version": 1}

    monkeypatch.setattr(local_sessions, "live_sessions_for_room", fake_live)
    metrics, ledger, posted = _capture_metrics(monkeypatch), {}, {}
    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {}),
        event_kind="user_request",
        metrics=metrics,
        ledger=ledger,
        posted=posted,
        body=fake_body,
    )
    assert seen["live"] == ("owner-1", _ROOM)
    assert seen["sessions"] == sent


@pytest.mark.asyncio
async def test_dispatch_sends_no_summary_into_a_room_with_a_foreign_member(monkeypatch):
    """A shared vendor-room member carries ``homeOwnerId`` from another org --
    session titles must not leak across that boundary (spec #2644). The guard
    lives inside live_sessions_for_room now (see test_local_sessions.py), so
    it runs unmocked here rather than being asserted un-called."""
    seen: dict = {}

    async def fake_body(**kwargs):
        seen["sessions"] = kwargs.get("sessions")
        return {"version": 1}

    metrics, ledger, posted = _capture_metrics(monkeypatch), {}, {}
    foreign_members = [
        {"kind": "agent", "id": "a1", "displayName": "Friday", "listenMode": "active"},
        {"kind": "human", "id": "vendor-u1", "displayName": "Vendor Rep", "homeOwnerId": "other-owner"},
    ]
    await _dispatch(
        monkeypatch,
        answer=_Answer(202, {}),
        event_kind="user_request",
        metrics=metrics,
        ledger=ledger,
        posted=posted,
        body=fake_body,
        members=foreign_members,
    )
    assert seen["sessions"] == []
