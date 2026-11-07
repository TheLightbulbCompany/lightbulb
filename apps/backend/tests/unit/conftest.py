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
