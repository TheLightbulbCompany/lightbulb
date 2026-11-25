import asyncio

import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_sets_flag_and_is_bounded(monkeypatch):
    import main
    from core.middleware import shutdown_aware

    shutdown_aware.set_shutting_down(False)

    monkeypatch.setattr("core.voice.os_voice.os_voice_sweeper", lambda stop: asyncio.sleep(3600))

    async with main.lifespan(main.app):
        assert shutdown_aware.is_shutting_down() is False
    # After exit, shutdown ran: set_shutting_down(True) is the first thing the
    # shutdown block does, and the bounded drain completes within budget.
    assert shutdown_aware.is_shutting_down() is True
