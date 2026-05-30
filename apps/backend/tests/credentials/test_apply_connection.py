import pytest
from core.repositories import connection_repo


@pytest.fixture
def fake_table(monkeypatch):
    store = {}

    class _T:
        def put_item(self, Item):
            store[(Item["owner_id"], Item["provider"])] = Item

        def get_item(self, Key):
            it = store.get((Key["owner_id"], Key["provider"]))
            return {"Item": it} if it else {}

        def delete_item(self, Key):
            store.pop((Key["owner_id"], Key["provider"]), None)

        def query(self, **kw):
            return {"Items": list(store.values())}

    monkeypatch.setattr(connection_repo, "_table", lambda: _T())
    return store


@pytest.mark.asyncio
async def test_roundtrip_encrypts_token(fake_table):
    await connection_repo.put("org_1", "linear", {"token": "lin_secret"}, status="connected")
    rec = await connection_repo.get("org_1", "linear")
    assert rec["status"] == "connected"
    assert connection_repo.decrypt_credential(rec) == {"token": "lin_secret"}
    assert "lin_secret" not in rec["credential_enc"]  # stored ciphertext, not plaintext


@pytest.mark.asyncio
async def test_delete(fake_table):
    await connection_repo.put("org_1", "linear", {"token": "x"})
    await connection_repo.delete("org_1", "linear")
    assert await connection_repo.get("org_1", "linear") is None


@pytest.mark.asyncio
async def test_list_for_owner(fake_table):
    await connection_repo.put("org_1", "linear", {"token": "a"})
    await connection_repo.put("org_1", "stripe", {"token": "b"})
    rows = await connection_repo.list_for_owner("org_1")
    assert {r["provider"] for r in rows} == {"linear", "stripe"}


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
