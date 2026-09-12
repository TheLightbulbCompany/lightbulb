"""AppSync live-publish failures must be countable and separable by class:
a 4xx is our bug, a 429 is shed load, a 5xx/transport is AppSync being down."""

import asyncio
import threading

import pytest
from botocore.credentials import Credentials

from core.config import settings
from core.services import live_events
from core.services.live_events import LiveEventPublisher, LivePublishError
from core.services.messaging import LiveEvent, make_live_event


class _Answer:
    def __init__(self, status_code):
        self.status_code = status_code

    def json(self):
        return {"successful": [{"identifier": "1"}], "failed": []}


async def _publish(monkeypatch, answers, metrics):
    """Run one ``_publish`` against a scripted sequence of responses/exceptions."""
    monkeypatch.setattr(settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://appsync.test/event")
    monkeypatch.setattr(live_events, "put_metric", lambda name, **kwargs: metrics.append((name, kwargs)))
    attempts = iter(answers)

    class FakeHttp:
        async def post(self, *_args, **_kwargs):
            answer = next(attempts)
            if isinstance(answer, Exception):
                raise answer
            return answer

    publisher = LiveEventPublisher(
        http_client=FakeHttp(),
        credentials=Credentials("AKIATEST", "secret").get_frozen_credentials(),
    )
    event = LiveEvent.model_validate(make_live_event("runtime.operation.changed", {"state": "succeeded"}))
    await publisher._publish("/users/11111111-1111-4111-8111-111111111111", event)


@pytest.mark.asyncio
async def test_client_error_is_counted_instead_of_failing_silently(monkeypatch):
    """The non-retried 4xx raise was the one publish path that emitted nothing,
    so a malformed channel looked identical to a healthy queue."""
    metrics: list[tuple[str, dict]] = []

    with pytest.raises(LivePublishError, match="AppSync HTTP 403"):
        await _publish(monkeypatch, [_Answer(403)], metrics)

    assert metrics == [("messaging.live.publish", {"dimensions": {"outcome": "error", "status_class": "4xx"}})]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answers", "status_class"),
    [
        ([_Answer(429), _Answer(429)], "429"),
        ([_Answer(503), _Answer(503)], "5xx"),
        ([ConnectionError("reset"), ConnectionError("reset")], "transport"),
    ],
)
async def test_retry_exhaustion_reports_the_failure_class(monkeypatch, answers, status_class):
    metrics: list[tuple[str, dict]] = []

    with pytest.raises(LivePublishError, match="AppSync live publish failed"):
        await _publish(monkeypatch, answers, metrics)

    assert metrics == [("messaging.live.publish", {"dimensions": {"outcome": "error", "status_class": status_class}})]


@pytest.mark.asyncio
async def test_success_after_one_retry_reports_ok_only(monkeypatch):
    metrics: list[tuple[str, dict]] = []

    await _publish(monkeypatch, [_Answer(503), _Answer(200)], metrics)

    assert metrics == [("messaging.live.publish", {"dimensions": {"outcome": "ok"}})]


@pytest.mark.asyncio
async def test_task_credentials_are_reused_without_blocking_the_event_loop(monkeypatch):
    monkeypatch.setattr(settings, "APPSYNC_EVENTS_HTTP_ENDPOINT", "https://appsync.test/event")
    monkeypatch.setattr(live_events, "put_metric", lambda *_args, **_kwargs: None)
    release = threading.Event()
    order: list[str] = []

    class BlockingSession:
        def get_credentials(self):
            assert release.wait(timeout=1)
            order.append("credentials")
            return Credentials("AKIATEST", "secret")

    class FakeHttp:
        async def post(self, *_args, **_kwargs):
            return _Answer(200)

    monkeypatch.setattr(live_events, "get_session", lambda: BlockingSession())
    publisher = LiveEventPublisher(http_client=FakeHttp())
    event = LiveEvent.model_validate(make_live_event("runtime.operation.changed", {"state": "succeeded"}))
    timer = threading.Timer(0.1, release.set)
    timer.start()
    try:
        publish = asyncio.create_task(publisher._publish("/users/11111111-1111-4111-8111-111111111111", event))
        await asyncio.sleep(0)
        order.append("event-loop")
        await publish
        await publisher._publish("/users/11111111-1111-4111-8111-111111111111", event)
    finally:
        release.set()
        timer.cancel()

    assert order == ["event-loop", "credentials"]
