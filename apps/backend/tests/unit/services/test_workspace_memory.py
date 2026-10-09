"""The workspace-memory wipe: the owner's records now (loudly), then a background tail that deletes
the owner's events and re-sweeps records extraction produced late -- and nothing of anyone else's."""

import asyncio
from unittest.mock import MagicMock

import pytest

from core.services import workspace_memory


def _fake_client(record_pages: list[tuple[list[str], bool]], sessions: dict[str, list[list[str]]]) -> MagicMock:
    """``record_pages``: (ids, has_next) per ListMemoryRecords call, in order. ``sessions``: event
    pages per session."""
    fake = MagicMock()
    fake.list_memory_records.side_effect = [
        {"memoryRecordSummaries": [{"memoryRecordId": r} for r in page], **({"nextToken": "n"} if nxt else {})}
        for page, nxt in record_pages
    ]
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": []}
    fake.list_sessions.return_value = {"sessionSummaries": [{"sessionId": s} for s in sessions]}
    pages = {sid: list(ps) for sid, ps in sessions.items()}

    def _list_events(**kw):
        page = pages[kw["sessionId"]].pop(0)
        more = bool(pages[kw["sessionId"]])
        return {"events": [{"eventId": e} for e in page], **({"nextToken": "t"} if more else {})}

    fake.list_events.side_effect = _list_events
    fake.list_actors.return_value = {"actorSummaries": []}
    return fake


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)


@pytest.mark.asyncio
async def test_wipe_deletes_records_now_and_events_plus_late_records_in_the_tail(monkeypatch, configured):
    records = [f"r{i}" for i in range(250)]
    fake = _fake_client(
        # first pass: team in two pages, member prefix empty; the late re-sweep finds one record
        # extraction produced afterwards
        [(records[:125], True), (records[125:], False), ([], False), (["late"], False), ([], False)],
        {"s1": [["e1", "e2"], ["e3"]], "s2": [["e4"]]},
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    # Records are gone before the call returns; events are the tail's job.
    assert fake.batch_delete_memory_records.call_count == 3
    await asyncio.gather(*workspace_memory._tails)

    for call in fake.list_memory_records.call_args_list:
        # Team namespace, then the member-scope prefix; never namespacePath.
        assert call.kwargs["namespace"] in ("/owners/org_x/", "/owners/org_x:user_")
        assert "namespacePath" not in call.kwargs
    chunks = [c.kwargs["records"] for c in fake.batch_delete_memory_records.call_args_list]
    assert [len(c) for c in chunks] == [100, 100, 50, 1]
    assert [r["memoryRecordId"] for c in chunks for r in c] == [*records, "late"]
    fake.list_sessions.assert_called_once_with(memoryId="mem-1", actorId="org_x")
    deleted = {(c.kwargs["sessionId"], c.kwargs["eventId"]) for c in fake.delete_event.call_args_list}
    # Multi-page sessions are fully listed BEFORE deletion starts, so nothing is skipped.
    assert deleted == {("s1", "e1"), ("s1", "e2"), ("s1", "e3"), ("s2", "e4")}
    assert all(c.kwargs["actorId"] == "org_x" for c in fake.delete_event.call_args_list)


@pytest.mark.asyncio
async def test_a_record_that_fails_to_delete_fails_the_wipe(monkeypatch, configured):
    """Records never expire: a partial delete must say 'retry me', not report success."""
    fake = _fake_client([(["r1", "r2"], False), ([], False)], {})
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": [{"id": "r2"}]}
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)
    with pytest.raises(RuntimeError, match="1 of 2 records"):
        await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)


@pytest.mark.asyncio
async def test_wipe_is_a_no_op_when_memory_is_not_configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("no AWS call without a memory"))
    await workspace_memory.delete_owner_memory("org_x")


@pytest.mark.asyncio
async def test_wipe_refuses_an_owner_id_that_would_widen_the_namespace(monkeypatch, configured):
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("must refuse before any call"))
    for bad in ("", "org_x/", "../org_y", "org_x:user_a"):
        with pytest.raises(ValueError):
            await workspace_memory.delete_owner_memory(bad)


def _scoped_client(records_by_ns: dict[str, list[str]], actors: list[str]) -> MagicMock:
    """Records per namespace (a prefix list returns every namespace that starts with it, as the
    API documents), one session with one event per actor."""
    fake = MagicMock()
    fake.list_memory_records.side_effect = lambda **kw: {
        "memoryRecordSummaries": [
            {"memoryRecordId": r} for ns, ids in records_by_ns.items() if ns.startswith(kw["namespace"]) for r in ids
        ]
    }
    fake.batch_delete_memory_records.return_value = {"failedRecords": []}
    fake.list_actors.return_value = {"actorSummaries": [{"actorId": a} for a in actors]}
    fake.list_sessions.return_value = {"sessionSummaries": [{"sessionId": "s"}]}
    fake.list_events.side_effect = lambda **kw: {"events": [{"eventId": f"e-{kw['actorId']}"}]}
    return fake


def _deleted_records(fake) -> list[str]:
    return [r["memoryRecordId"] for c in fake.batch_delete_memory_records.call_args_list for r in c.kwargs["records"]]


@pytest.mark.asyncio
async def test_owner_wipe_also_deletes_every_member_scope_and_nothing_of_a_prefix_neighbour(monkeypatch, configured):
    """Member scopes come from the namespace prefix (reaches a member whose events already
    expired, so ListActors no longer lists them) and from ListActors (their events, plus the exact
    namespace); an id seen twice is deleted once. org_xy shares the text prefix and is untouched."""
    fake = _scoped_client(
        {
            "/owners/org_x/": ["team"],
            "/owners/org_x:user_a/": ["a1"],
            "/owners/org_x:user_gone/": ["gone1"],  # events expired: not in ListActors
            "/owners/org_xy/": ["xy"],
            "/owners/org_xy:user_b/": ["xyb"],
        },
        actors=["org_x", "org_x:user_a", "org_xy", "org_xy:user_b"],
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    first = _deleted_records(fake)
    assert sorted(first) == ["a1", "gone1", "team"]
    await asyncio.gather(*workspace_memory._tails)

    deleted_actors = {c.kwargs["actorId"] for c in fake.delete_event.call_args_list}
    assert deleted_actors == {"org_x", "org_x:user_a"}
    assert not any("xy" in r for r in _deleted_records(fake))


@pytest.mark.asyncio
async def test_member_wipe_deletes_only_that_members_private_scope(monkeypatch, configured):
    fake = _scoped_client(
        {"/owners/org_x/": ["team"], "/owners/org_x:user_a/": ["a1"], "/owners/org_x:user_ab/": ["ab1"]},
        actors=["org_x", "org_x:user_a", "org_x:user_ab"],
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    await asyncio.gather(*workspace_memory._tails)

    # The exact namespace with its trailing "/": never user_ab's, never the team's.
    assert {c.kwargs["namespace"] for c in fake.list_memory_records.call_args_list} == {"/owners/org_x:user_a/"}
    assert _deleted_records(fake) == ["a1", "a1"]  # now, and the late re-sweep (fake never empties)
    assert {c.kwargs["actorId"] for c in fake.delete_event.call_args_list} == {"org_x:user_a"}
    fake.list_actors.assert_not_called()


@pytest.mark.asyncio
async def test_member_wipe_skips_ids_that_can_have_no_private_scope(monkeypatch, configured):
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("no AWS call"))
    for user_id in ("", "member", "agent-ceo", "user_a/../x", "user_a:b"):
        await workspace_memory.delete_member_memory("org_x", user_id)
    with pytest.raises(ValueError):
        await workspace_memory.delete_member_memory("org_x:user_z", "user_a")
