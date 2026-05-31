import pytest


@pytest.mark.asyncio
async def test_apply_openclaw_paths_injects_literal(monkeypatch):
    from core.services.credentials import apply_connection as ac

    calls = []

    async def _fake_set(owner, path, value):
        calls.append((owner, path, value))

    monkeypatch.setattr(ac.config_patcher, "set_openclaw_config_path", _fake_set)
    await ac.apply_connection("org_1", "linear", {"token": "lin_secret"})
    assert calls == [("org_1", ["mcp", "servers", "linear", "env", "LINEAR_API_KEY"], "lin_secret")]


@pytest.mark.asyncio
async def test_apply_materializer_dispatch_via_thread(monkeypatch):
    # google wiring dispatches to the gog_cli materializer, run off the event loop
    # via run_in_thread (so the blocking subprocess/HTTP doesn't stall the loop).
    from core.services.credentials import apply_connection as ac

    seen = {}

    def _fake_mat(owner, provider, cred):
        seen.update(owner=owner, provider=provider, cred=cred)

    threaded = {}

    async def _fake_run_in_thread(fn, *args, **kwargs):
        threaded["called"] = True
        return fn(*args, **kwargs)

    monkeypatch.setitem(ac.MATERIALIZERS, "gog_cli", _fake_mat)
    monkeypatch.setattr(ac, "run_in_thread", _fake_run_in_thread)
    await ac.apply_connection("org_1", "google", {"access_token": "a", "refresh_token": "r"})
    assert threaded.get("called") is True
    assert seen["owner"] == "org_1" and seen["cred"]["refresh_token"] == "r"
