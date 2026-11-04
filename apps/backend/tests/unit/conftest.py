import pytest


@pytest.fixture(autouse=True)
def _hints_drained_inline(request, monkeypatch):
    """publish_resources_changed fans out in a background task since #3909. Tests across the tree
    assert the hint right after the call that caused it, so drain it inline for them; the two
    tests of the background behaviour itself opt out by name."""
    name = request.node.name
    if "off_the_callers_frame" in name or "coalesce" in name:
        return
    import asyncio as _asyncio

    from core.services import local_session_relay as _relay

    real = _relay.publish_resources_changed

    async def drained(*args, **kwargs):
        await real(*args, **kwargs)
        await _asyncio.gather(*list(_relay._hint_tasks))

    monkeypatch.setattr(_relay, "publish_resources_changed", drained)


@pytest.fixture(autouse=True)
def _apps_tasks_end_with_their_test():
    """``apps_service.drive``/``watch`` keep one background task per app in module dicts; one armed by a
    test (through ``status()``, say) must not outlive it and hold that app's slot in the next test's loop."""
    yield
    import sys
    from contextlib import suppress

    svc = sys.modules.get("core.services.apps_service")
    if svc is None:
        return
    for tasks in (svc._drives, svc._watches):
        for task in list(tasks.values()):
            with suppress(RuntimeError):  # its loop may already be closed
                task.cancel()
        tasks.clear()
