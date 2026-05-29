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
