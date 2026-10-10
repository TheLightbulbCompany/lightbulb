"""Memory review: a viewer lists only their own private scope and team, and can delete only there."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import workspace_memory as wm

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 2, tzinfo=timezone.utc)
PRIV, TEAM = "/owners/org_a:user_me/", "/owners/org_a/"


def _rec(rid, ns, text="fact", at=T0):
    return {
        "memoryRecordId": rid,
        "namespaces": [ns],
        "content": {"text": text},
        "createdAt": at,
        "memoryStrategyId": "workspace_facts-x",
    }


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(wm.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)
    client = MagicMock()
    client.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    monkeypatch.setattr(wm, "_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_lists_private_and_team_newest_first_and_drops_prefix_neighbours(fake):
    by_ns = {
        PRIV: [
            _rec("mem-p1", PRIV, "old", T0),
            _rec("mem-p2", PRIV, "new", T1),
            _rec("mem-x", "/owners/org_a:user_mex/"),
        ],  # a prefix neighbour: never shown
        TEAM: [_rec("mem-t1", TEAM, wm.CONFIRMED_PREFIX + "team", T0)],
    }
    fake.list_memory_records.side_effect = lambda **kw: {"memoryRecordSummaries": by_ns[kw["namespace"]]}
    out = await wm.list_memories("org_a", "user_me")
    assert [m["id"] for m in out["private"]] == ["mem-p2", "mem-p1"]
    assert out["team"] == [{"id": "mem-t1", "text": "team", "createdAt": T0.isoformat(), "confirmed": True}]
    assert out["private"][0]["confirmed"] is False
    assert out["truncated"] is False
    assert {c.kwargs["namespace"] for c in fake.list_memory_records.call_args_list} == {PRIV, TEAM}


@pytest.mark.asyncio
async def test_list_caps_each_scope_and_says_so(fake, monkeypatch):
    monkeypatch.setattr(wm, "_LIST_CAP", 2)
    fake.list_memory_records.side_effect = lambda **kw: {
        "memoryRecordSummaries": [_rec(f"mem-{i}", kw["namespace"]) for i in range(3)],
        "nextToken": "n",
    }
    out = await wm.list_memories("org_a", "user_me")
    assert len(out["private"]) == 2 and out["truncated"] is True


@pytest.mark.asyncio
async def test_no_memory_configured_is_empty_and_never_builds_a_client(monkeypatch):
    monkeypatch.setattr(wm.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(wm, "_client", lambda: pytest.fail("no client without a memory"))
    assert await wm.list_memories("org_a", "user_me") == {"private": [], "team": [], "truncated": False}
    with pytest.raises(LookupError):
        await wm.delete_memory("org_a", "user_me", "mem-" + "a" * 40)


@pytest.mark.asyncio
@pytest.mark.parametrize("ns", [PRIV, TEAM])
async def test_deletes_a_record_in_the_viewers_scopes(fake, ns):
    fake.get_memory_record.return_value = {"memoryRecord": _rec("mem-1", ns)}
    await wm.delete_memory("org_a", "user_me", "mem-1")
    fake.batch_delete_memory_records.assert_called_once_with(memoryId="mem-1", records=[{"memoryRecordId": "mem-1"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("ns", ["/owners/org_a:user_other/", "/owners/org_b/", "/owners/org_a/deeper/"])
async def test_refuses_another_members_or_workspaces_record(fake, ns):
    fake.get_memory_record.return_value = {"memoryRecord": _rec("mem-1", ns)}
    with pytest.raises(LookupError):
        await wm.delete_memory("org_a", "user_me", "mem-1")
    fake.batch_delete_memory_records.assert_not_called()


@pytest.mark.asyncio
async def test_missing_record_is_lookup_error(fake):
    fake.get_memory_record.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetMemoryRecord"
    )
    with pytest.raises(LookupError):
        await wm.delete_memory("org_a", "user_me", "mem-1")


@pytest.mark.asyncio
async def test_a_failed_delete_raises(fake):
    fake.get_memory_record.return_value = {"memoryRecord": _rec("mem-1", TEAM)}
    fake.batch_delete_memory_records.return_value = {
        "successfulRecords": [],
        "failedRecords": [{"memoryRecordId": "mem-1"}],
    }
    with pytest.raises(RuntimeError):
        await wm.delete_memory("org_a", "user_me", "mem-1")
