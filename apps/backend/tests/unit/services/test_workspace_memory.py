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
    pages_left = [
        {"memoryRecordSummaries": [{"memoryRecordId": r} for r in page], **({"nextToken": "n"} if nxt else {})}
        for page, nxt in record_pages
    ]
    # The pages are the team facts namespace's; the member scans (namespacePath) and the turns,
    # entries and tasks namespaces hold nothing here.
    fake.list_memory_records.side_effect = lambda **kw: (
        {"memoryRecordSummaries": []}
        if "namespacePath" in kw or kw["namespace"] in ("/turns/org_x/", "/entries/org_x/", "/tasks/org_x/")
        else pages_left.pop(0)
    )
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


@pytest.fixture(autouse=True)
def no_workspace_rows(monkeypatch):
    """The owner wipe also deletes the workspace's session and draft rows (tests/unit/services/test_evaluator.py)."""
    from unittest.mock import AsyncMock

    from core.services import evaluator, session_store

    monkeypatch.setattr(session_store, "delete_workspace_sessions", AsyncMock(return_value=0))
    monkeypatch.setattr(evaluator, "delete_workspace_drafts", AsyncMock(return_value=0))


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-1", raising=False)


@pytest.mark.asyncio
async def test_wipe_deletes_records_now_and_events_plus_late_records_in_the_tail(monkeypatch, configured):
    records = [f"r{i}" for i in range(250)]
    fake = _fake_client(
        # first pass: team in two pages; the late re-sweep finds one record extraction produced afterwards
        [(records[:125], True), (records[125:], False), (["late"], False)],
        {"s1": [["e1", "e2"], ["e3"]], "s2": [["e4"]]},
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    # Records are gone before the call returns; events are the tail's job.
    assert fake.batch_delete_memory_records.call_count == 3
    await asyncio.gather(*workspace_memory._tails)

    for call in fake.list_memory_records.call_args_list:
        # The four team namespaces, plus one member scan per family.
        assert call.kwargs.get("namespace", "/owners/org_x/") in workspace_memory.namespaces_for("org_x", None).values()
        assert call.kwargs.get("namespacePath", "/owners/") in ("/owners/", "/turns/", "/tasks/")
    chunks = [c.kwargs["records"] for c in fake.batch_delete_memory_records.call_args_list]
    assert [len(c) for c in chunks] == [100, 100, 50, 1]
    assert [r["memoryRecordId"] for c in chunks for r in c] == [*records, "late"]
    fake.list_sessions.assert_called_once_with(memoryId="mem-1", actorId="org_x")
    deleted = {(c.kwargs["sessionId"], c.kwargs["eventId"]) for c in fake.delete_event.call_args_list}
    # Multi-page sessions are fully listed BEFORE deletion starts, so nothing is skipped.
    assert deleted == {("s1", "e1"), ("s1", "e2"), ("s1", "e3"), ("s2", "e4")}
    assert all(c.kwargs["actorId"] == "org_x" for c in fake.delete_event.call_args_list)


@pytest.mark.asyncio
async def test_owner_wipe_covers_four_namespaces(monkeypatch, configured):
    seen = {}

    async def spy(client, label, memory_id, namespaces, *a, **k):
        seen["namespaces"] = namespaces

    monkeypatch.setattr(workspace_memory, "_client", lambda: _scoped_client({}, actors=[]))
    monkeypatch.setattr(workspace_memory, "_wipe", spy)
    await workspace_memory.delete_owner_memory("org_x")
    wanted = workspace_memory.namespaces_for("org_x", None)
    assert set(wanted) == {"facts", "turns", "entries", "tasks"}
    assert set(wanted.values()) <= set(seen["namespaces"])


@pytest.mark.asyncio
async def test_a_record_that_fails_to_delete_fails_the_wipe(monkeypatch, configured):
    """Records never expire: a partial delete must say 'retry me', not report success."""
    fake = _fake_client([(["r1", "r2"], False)], {})
    fake.batch_delete_memory_records.return_value = {"successfulRecords": [], "failedRecords": [{"id": "r2"}]}
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)
    with pytest.raises(RuntimeError) as failed:
        await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    assert "1 of 2 records" in str(failed.value.__cause__)


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
    """Records per namespace as dev AgentCore answers (2026-10-11): ``namespace`` matches exactly
    (a trailing slash aside), ``namespacePath`` returns everything under it. One session with one
    event per actor."""
    fake = MagicMock()

    def _list(**kw):
        if "namespacePath" in kw:
            hits = [(ns, ids) for ns, ids in records_by_ns.items() if ns.startswith(kw["namespacePath"])]
        else:
            hits = [(ns, ids) for ns, ids in records_by_ns.items() if ns.rstrip("/") == kw["namespace"].rstrip("/")]
        return {"memoryRecordSummaries": [{"memoryRecordId": r, "namespaces": [ns]} for ns, ids in hits for r in ids]}

    fake.list_memory_records.side_effect = _list
    fake.batch_delete_memory_records.return_value = {"failedRecords": []}
    fake.list_actors.return_value = {"actorSummaries": [{"actorId": a} for a in actors]}
    fake.list_sessions.return_value = {"sessionSummaries": [{"sessionId": "s"}]}
    fake.list_events.side_effect = lambda **kw: {"events": [{"eventId": f"e-{kw['actorId']}"}]}
    return fake


def _deleted_records(fake) -> list[str]:
    return [r["memoryRecordId"] for c in fake.batch_delete_memory_records.call_args_list for r in c.kwargs["records"]]


@pytest.mark.asyncio
async def test_owner_wipe_also_deletes_every_member_scope_and_nothing_of_a_prefix_neighbour(monkeypatch, configured):
    """Member scopes come from the /owners/ scan (reaches a member whose events already
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
@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_member_wipe_deletes_only_that_members_private_scope(monkeypatch, configured):
    fake = _scoped_client(
        {"/owners/org_x/": ["team"], "/owners/org_x:user_a/": ["a1"], "/owners/org_x:user_ab/": ["ab1"]},
        actors=["org_x", "org_x:user_a", "org_x:user_ab"],
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    await asyncio.gather(*workspace_memory._tails)

    # The exact namespaces with their trailing "/": never user_ab's, never the team's.
    assert {c.kwargs["namespace"] for c in fake.list_memory_records.call_args_list} == {
        "/owners/org_x:user_a/",
        "/turns/org_x:user_a/",
        "/tasks/org_x:user_a/",
    }
    assert _deleted_records(fake) == ["a1", "a1"]  # now, and the late re-sweep (fake never empties)
    assert {c.kwargs["actorId"] for c in fake.delete_event.call_args_list} == {"org_x:user_a"}
    fake.list_actors.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", [True, False, RuntimeError("ddb down")])
@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_member_tail_runs_only_while_the_member_is_still_gone(monkeypatch, configured, verdict):
    fake = _scoped_client({"/owners/org_x:user_a/": ["a1"]}, actors=["org_x:user_a"])
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    async def check():
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0, still_gone=check)
    await asyncio.gather(*workspace_memory._tails)

    # The immediate delete always happens; the late re-sweep and event deletion only if still gone.
    assert _deleted_records(fake) == (["a1", "a1"] if verdict is True else ["a1"])
    assert bool(fake.delete_event.call_args_list) is (verdict is True)


@pytest.mark.asyncio
async def test_member_wipe_skips_ids_that_can_have_no_private_scope(monkeypatch, configured):
    monkeypatch.setattr(workspace_memory, "_client", lambda: pytest.fail("no AWS call"))
    for user_id in ("", "member", "agent-ceo", "user_a/../x", "user_a:b"):
        await workspace_memory.delete_member_memory("org_x", user_id)
    with pytest.raises(ValueError):
        await workspace_memory.delete_member_memory("org_x:user_z", "user_a")


@pytest.fixture
def no_evaluator_wipe(monkeypatch):
    """The member wipe also clears sessions, drafts and episodes (tests/unit/services/test_evaluator.py)."""
    from unittest.mock import AsyncMock

    from core.services import evaluator, memory_api, network_login, session_store

    for mod, name in (
        (session_store, "delete_sessions"),
        (evaluator, "delete_drafts"),
        (evaluator, "delete_episodes"),
        (network_login, "computer_keys"),  # no signed-in computer: no forget_author forward
        (network_login, "forget_keys"),
        (memory_api, "forget_on_behalf_of"),
    ):
        monkeypatch.setattr(mod, name, AsyncMock(return_value=[] if name == "computer_keys" else 0))


@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_member_wipe_runs_every_step_when_one_fails(monkeypatch):
    from core.services import evaluator, memory_api, session_store

    ran = []

    async def boom(*a, **k):
        ran.append("sessions")
        raise OSError("down")

    async def ok(name):
        ran.append(name)
        return 0

    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "mem-test-000001", raising=False)
    monkeypatch.setattr(workspace_memory, "_wipe", lambda *a, **k: ok("wipe"))
    monkeypatch.setattr(workspace_memory, "_client", MagicMock())
    monkeypatch.setattr(session_store, "delete_sessions", boom)
    monkeypatch.setattr(evaluator, "delete_drafts", lambda u, **k: ok("drafts"))
    monkeypatch.setattr(memory_api, "forget_on_behalf_of", lambda o, u: ok("on behalf"))
    with pytest.raises(RuntimeError, match="OSError"):
        await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    assert ran == ["wipe", "sessions", "drafts", "on behalf"]


@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_member_wipe_forwards_forget_author(monkeypatch):
    from core.services import memory_api, network_login, session_store

    sent = []

    async def boom(*a, **k):
        raise OSError("down")

    # k1 is linked now; k2 only in the registry (offline, never signed out); k3 signed out.
    async def keys(user_id):
        assert user_id == "user_a"
        return ["k1", "k2", "k3"]

    async def forward(owner_id, author):
        sent.append((owner_id, author))

    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    monkeypatch.setattr(session_store, "delete_sessions", boom)  # an earlier step raises
    monkeypatch.setattr(network_login, "computer_keys", keys)
    monkeypatch.setattr(memory_api, "forget_author", forward)
    with pytest.raises(RuntimeError, match="OSError"):
        await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    assert sent == [("org_x", "k1"), ("org_x", "k2"), ("org_x", "k3")]


async def test_owner_wipe_deletes_the_workspaces_sessions_and_drafts_without_memory(monkeypatch):
    """Rows and objects go even in an env with no memory, and without any member webhook."""
    from core.services import evaluator, session_store

    monkeypatch.setattr(workspace_memory.settings, "AGENTCORE_MEMORY_ID", "", raising=False)
    await workspace_memory.delete_owner_memory("org_x")
    session_store.delete_workspace_sessions.assert_awaited_once_with("org_x")
    evaluator.delete_workspace_drafts.assert_awaited_once_with("org_x")


async def test_forget_on_behalf_of_pages_the_log_and_forgets_only_that_persons_entries():
    from unittest.mock import AsyncMock, patch

    from core.services import memory_api

    def row(seq, eid, who, t="remember"):
        op = {"t": t, "subject": "s", "fact": "f", "kind": "fact", "scope": "me",
              "source": {"harness": "workspace", "on_behalf_of": who}}  # fmt: skip
        return {"id": eid, "author": "ws", "seq": seq, "op": op, "at": 1}

    pages = {
        0: {"entries": [row(1, "a1", "user_a"), row(2, "b1", "user_b"),
                        {"id": "x", "author": "ws", "tombstone": True, "seq": 3}], "next": 3},
        3: {"entries": [row(4, "a2", "user_a")], "next": 4},
        4: {"entries": [], "next": 4},
    }  # fmt: skip

    async def call_op(owner_id, op, params, timeout=30.0):
        if op == "memory.list":
            return pages[params["since_seq"]]
        return {}

    forget = AsyncMock()
    with patch.object(memory_api.network_member, "call_op", call_op), patch.object(memory_api, "forget", forget):
        assert await memory_api.forget_on_behalf_of("org_x", "user_a") == 2
    assert [c.args for c in forget.await_args_list] == [("org_x", "a1"), ("org_x", "a2")]

    parked = AsyncMock(return_value=None)
    with patch.object(memory_api.network_member, "call_op", parked), pytest.raises(RuntimeError, match="parked"):
        await memory_api.forget_on_behalf_of("org_x", "user_a")


def test_memory_tab_lists_a_validto_zero_claim_as_current():
    """The extraction model writes validTo 0 for "empty"; the tab must not call that replaced."""
    from datetime import UTC, datetime

    rec = {
        "memoryRecordId": "z",
        "content": {"text": "Ann: city: Oslo"},
        "createdAt": datetime(2026, 2, 1, tzinfo=UTC),
        "metadata": {"kind": {"stringValue": "claim"}, "validTo": {"numberValue": 0}},
    }
    assert workspace_memory._view(rec)["status"] == "current"


def test_namespaces_for_adds_task_and_private_namespaces():
    """Super-agent memory (spec 07 §3): task cards in /tasks/<o>/, and a person's private lane in
    /turns/<o>:<u>/ and /tasks/<o>:<u>/. Each is a sibling of the facts namespace, never a child."""
    team = workspace_memory.namespaces_for("org_x", None)
    assert team == {
        "facts": "/owners/org_x/",
        "turns": "/turns/org_x/",
        "entries": "/entries/org_x/",
        "tasks": "/tasks/org_x/",
    }
    mine = workspace_memory.namespaces_for("org_x", "user_a")
    assert mine == {
        **team,
        "private": "/owners/org_x:user_a/",
        "private_turns": "/turns/org_x:user_a/",
        "private_tasks": "/tasks/org_x:user_a/",
    }
    # ":" only between the owner and the member: a team namespace never names a member.
    assert all(":" not in ns for ns in team.values())
    assert all(ns.count(":") == 1 for k, ns in mine.items() if k.startswith("private"))


@pytest.mark.asyncio
async def test_owner_wipe_covers_tasks_and_private_turns_and_private_tasks(monkeypatch, configured):
    """Member lanes are found by a namespacePath scan of each family (/owners/, /turns/, /tasks/)
    and by ListActors; a prefix neighbour (org_xy, another owner) keeps everything."""
    fake = _scoped_client(
        {
            "/owners/org_x/": ["team"],
            "/tasks/org_x/": ["card"],
            "/tasks/org_x:user_a/": ["pcard"],
            "/turns/org_x:user_a/": ["pturn"],
            "/turns/org_x:user_gone/": ["goneturn"],  # events expired: not in ListActors
            "/tasks/other/": ["other"],
            "/tasks/org_xy:user_b/": ["xyb"],
        },
        actors=["org_x", "org_x:user_a"],
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_owner_memory("org_x", resweep_after_s=0)
    await asyncio.gather(*workspace_memory._tails)

    assert set(_deleted_records(fake)) == {"team", "card", "pcard", "pturn", "goneturn"}
    scanned = {
        c.kwargs["namespacePath"] for c in fake.list_memory_records.call_args_list if "namespacePath" in c.kwargs
    }
    assert scanned == {"/owners/", "/turns/", "/tasks/"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("no_evaluator_wipe")
async def test_member_wipe_covers_private_turns_and_private_tasks(monkeypatch, configured):
    fake = _scoped_client(
        {
            "/owners/org_x/": ["team"],
            "/turns/org_x/": ["tturn"],
            "/tasks/org_x/": ["tcard"],
            "/owners/org_x:user_a/": ["a1"],
            "/turns/org_x:user_a/": ["a2"],
            "/tasks/org_x:user_a/": ["a3"],
            "/tasks/org_x:user_ab/": ["ab"],
        },
        actors=["org_x", "org_x:user_a"],
    )
    monkeypatch.setattr(workspace_memory, "_client", lambda: fake)

    await workspace_memory.delete_member_memory("org_x", "user_a", resweep_after_s=0)
    await asyncio.gather(*workspace_memory._tails)

    assert set(_deleted_records(fake)) == {"a1", "a2", "a3"}  # team namespaces and user_ab untouched
    assert {c.kwargs["namespace"] for c in fake.list_memory_records.call_args_list} == {
        "/owners/org_x:user_a/",
        "/turns/org_x:user_a/",
        "/tasks/org_x:user_a/",
    }
