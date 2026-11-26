import pytest


@pytest.mark.asyncio
async def test_lifespan_shutdown_stops_the_sweeper(monkeypatch):
    import main

    stopped = []

    async def sweeper(stop):
        await stop.wait()
        stopped.append(True)

    monkeypatch.setattr("core.voice.os_voice.os_voice_sweeper", sweeper)
    async with main.lifespan(main.app):
        pass
    assert stopped == [True]
