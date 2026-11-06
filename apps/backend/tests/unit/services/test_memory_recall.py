"""Recall: expand, search AgentCore several ways, fuse, rerank, compose -- against a recording client
whose every call is validated against the real bedrock-agentcore request shape."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import boto3
import pytest
from botocore.validate import validate_parameters

from core.services import memory_models, memory_recall, workspace_memory
from tests.unit.services.agentcore_filters import check_filters

MODEL = boto3.client("bedrock-agentcore", region_name="us-east-1").meta.service_model
NS = workspace_memory.namespaces_for("own1", "user_a")
TEAM_ONLY = workspace_memory.namespaces_for("own1", None)
MEM = "mem-1234567890"
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def _meta(**kw):
    return {k: ({"numberValue": v} if isinstance(v, (int, float)) else {"stringValue": v}) for k, v in kw.items()}


def _rec(rid, text, ts="2026-02-01T00:00:00Z", **meta):
    """A MemoryRecordSummary as Retrieve/ListMemoryRecords return it (``createdAt``, no ``timestamp``)."""
    out = {
        "memoryRecordId": rid,
        "content": {"text": text},
        "memoryStrategyId": "s",
        "namespaces": ["/x/"],
        "createdAt": datetime.fromisoformat(ts),
    }
    return {**out, "metadata": _meta(**meta)} if meta else out


SUMMARY = MODEL.shape_for("MemoryRecordSummary")


class FakeClient:
    """``handler(op, kwargs) -> list[summary]``; records every validated call."""

    def __init__(self, handler=lambda op, kw: []):
        self.handler, self.calls = handler, []

    def _do(self, op, shape, kw):
        self.calls.append((op, kw))
        validate_parameters(kw, MODEL.operation_model(shape).input_shape)
        check_filters(kw.get("metadataFilters") or kw.get("searchCriteria", {}).get("metadataFilters"), shape)
        rows = self.handler(op, kw)
        for row in rows:  # what the service returns: every required member, nothing it never sends
            assert set(SUMMARY.required_members) <= set(row) <= set(SUMMARY.members), row
        return {"memoryRecordSummaries": rows}

    def retrieve_memory_records(self, **kw):
        return self._do("retrieve", "RetrieveMemoryRecords", kw)

    def list_memory_records(self, **kw):
        return self._do("list", "ListMemoryRecords", kw)

    def where(self, op, namespace):
        return [kw for o, kw in self.calls if o == op and kw["namespace"] == namespace]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    memory_recall._entity_cache.clear()

    async def no_expand(question):
        return [question]

    async def identity_rerank(query, documents, top_n):
        return [(i, 1.0 - i / 1000) for i in range(min(top_n, len(documents)))]

    monkeypatch.setattr(memory_models, "expand", no_expand)
    monkeypatch.setattr(memory_models, "rerank", identity_rerank)


def _use(monkeypatch, client):
    monkeypatch.setattr(workspace_memory, "_client", lambda: client)
    return client


def _run(**kw):
    return memory_recall.recall(memory_id=MEM, namespaces=kw.pop("namespaces", NS), question="q?", **kw)


def _texts(page):
    return [r["content"] for r in page["results"]]


def test_namespaces_for():
    assert NS == {
        "facts": "/owners/own1/",
        "turns": "/turns/own1/",
        "entries": "/entries/own1/",
        "tasks": "/tasks/own1/",
        "private": "/owners/own1:user_a/",
        "private_turns": "/turns/own1:user_a/",
        "private_tasks": "/tasks/own1:user_a/",
    }
    assert not any(k.startswith("private") for k in TEAM_ONLY)


@pytest.mark.asyncio
async def test_recall_empty_namespace_returns_empty_page(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    page = await _run(as_of=AS_OF)
    assert page["results"] == [] and page["asOf"] == AS_OF.isoformat()
    assert c.calls


@pytest.mark.asyncio
async def test_recall_only_queries_own_namespaces(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    await _run(namespaces=TEAM_ONLY, include_private=True)
    assert c.calls
    assert {kw["namespace"] for _, kw in c.calls} <= set(TEAM_ONLY.values())
    assert all(kw["searchCriteria"]["topK"] == 100 for o, kw in c.calls if o == "retrieve")


@pytest.mark.asyncio
async def test_recall_private_only_when_included(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    await _run()
    assert not c.where("retrieve", NS["private"])
    await _run(include_private=True)
    assert len(c.where("retrieve", NS["private"])) == 1


@pytest.mark.asyncio
async def test_recall_as_of_filters_are_sent(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    await _run(as_of=AS_OF)
    e = AS_OF.timestamp()
    start = {
        "left": {"metadataKey": "validFrom"},
        "operator": "LESS_THAN_OR_EQUALS",
        "right": {"metadataValue": {"numberValue": e}},
    }
    open_ended = {"left": {"metadataKey": "validTo"}, "operator": "NOT_EXISTS"}
    later = {
        "left": {"metadataKey": "validTo"},
        "operator": "GREATER_THAN",
        "right": {"metadataValue": {"numberValue": e}},
    }
    zero = {
        "left": {"metadataKey": "validTo"},
        "operator": "EQUALS_TO",
        "right": {"metadataValue": {"numberValue": 0.0}},
    }
    sent = [kw["searchCriteria"]["metadataFilters"] for kw in c.where("retrieve", NS["facts"])]
    for status in ("current", "contested"):
        s = {
            "left": {"metadataKey": "status"},
            "operator": "EQUALS_TO",
            "right": {"metadataValue": {"stringValue": status}},
        }
        assert [s, start, open_ended] in sent and [s, start, later] in sent and [s, start, zero] in sent
    assert len(sent) == 6


@pytest.mark.asyncio
async def test_recall_as_of_keeps_fact_replaced_after_it(monkeypatch):
    later = datetime(2026, 4, 1, tzinfo=UTC).timestamp()
    _use(
        monkeypatch,
        FakeClient(
            lambda op, kw: (
                [_rec("a", "Ann: city: Oslo", kind="claim", validTo=later)]
                if op == "retrieve" and kw["namespace"] == NS["facts"]
                else []
            )
        ),
    )
    assert _texts(await _run(as_of=AS_OF)) == ["Ann: city: Oslo"]


def _filtered(rows):
    """Facts rows the service would return for each request's metadata filters (all must hold)."""

    def holds(row, f):
        have = (row.get("metadata") or {}).get(f["left"]["metadataKey"])
        if f["operator"] == "NOT_EXISTS":
            return have is None
        if have is None:
            return False
        a, b = next(iter(have.values())), next(iter(f["right"]["metadataValue"].values()))
        return {"EQUALS_TO": a == b, "GREATER_THAN": a > b, "LESS_THAN_OR_EQUALS": a <= b}[f["operator"]]

    def handler(op, kw):
        if op != "retrieve" or kw["namespace"] != NS["facts"]:
            return []
        filters = kw["searchCriteria"].get("metadataFilters") or []
        return [r for r in rows if all(holds(r, f) for f in filters)]

    return handler


# The extraction model writes validTo 0 for a current claim: "empty" in a NUMBER field.
ZERO = _rec("z", "Ann: city: Oslo", kind="claim", status="current", validFrom=1.0, validTo=0)


@pytest.mark.asyncio
async def test_recall_as_of_drops_a_claim_said_after_it_whose_year_was_guessed(monkeypatch):
    # validFrom 2024 passes the service's validFrom <= as_of, but the claim was said after as_of.
    guessed = _rec(
        "g",
        "Ann: city: Rome",
        ts="2026-04-02T00:00:00Z",
        kind="claim",
        status="current",
        validFrom=datetime(2024, 4, 2, tzinfo=UTC).timestamp(),
    )
    _use(monkeypatch, FakeClient(_filtered([guessed, ZERO])))
    assert _texts(await _run(as_of=AS_OF)) == ["Ann: city: Oslo"]
    assert sorted(_texts(await _run())) == ["Ann: city: Oslo", "Ann: city: Rome"]


@pytest.mark.asyncio
async def test_recall_validto_zero_is_current_without_as_of(monkeypatch):
    _use(monkeypatch, FakeClient(_filtered([ZERO])))
    page = await _run()
    assert _texts(page) == ["Ann: city: Oslo"] and page["results"][0]["status"] == "current"


@pytest.mark.asyncio
async def test_recall_validto_zero_is_current_as_of_after_its_start(monkeypatch):
    _use(monkeypatch, FakeClient(_filtered([ZERO])))
    assert _texts(await _run(as_of=AS_OF)) == ["Ann: city: Oslo"]


@pytest.mark.asyncio
async def test_recall_as_of_still_excludes_a_fact_that_ended_before_it(monkeypatch):
    ended = _rec("e", "Ann: city: Rome", kind="claim", status="current", validFrom=1.0, validTo=2.0)
    _use(monkeypatch, FakeClient(_filtered([ended, ZERO])))
    assert _texts(await _run(as_of=AS_OF)) == ["Ann: city: Oslo"]


@pytest.mark.asyncio
async def test_recall_prefers_current_over_replaced(monkeypatch):
    old = _rec("old", "Ann: city: Oslo", kind="claim", validTo=1.0)
    new = _rec("new", "Ann: city: Rome", kind="claim", status="current")
    _use(
        monkeypatch,
        FakeClient(lambda op, kw: [old, new] if op == "retrieve" and kw["namespace"] == NS["facts"] else []),
    )
    assert _texts(await _run()) == ["Ann: city: Rome"]


@pytest.mark.asyncio
async def test_recall_without_rerank_uses_rrf_order(monkeypatch):
    async def boom(*a):
        raise AssertionError("rerank called")

    monkeypatch.setattr(memory_models, "rerank", boom)
    rows = [_rec(f"t{i}", f"note {i}") for i in range(3)]
    _use(monkeypatch, FakeClient(lambda op, kw: rows if kw["namespace"] == NS["turns"] else []))
    assert _texts(await _run(use_rerank=False)) == ["note 0", "note 1", "note 2"]


@pytest.mark.asyncio
async def test_recall_rerank_failure_keeps_rrf_order(monkeypatch):
    async def fail(*a):
        return None

    monkeypatch.setattr(memory_models, "rerank", fail)
    rows = [_rec(f"t{i}", f"note {i}") for i in range(2)]
    _use(monkeypatch, FakeClient(lambda op, kw: rows if kw["namespace"] == NS["turns"] else []))
    assert _texts(await _run()) == ["note 0", "note 1"]


@pytest.mark.asyncio
async def test_recall_multi_part_question_queries_each_part(monkeypatch):
    async def two(question):
        return ["where does Ann live", "what does Bob eat"]

    monkeypatch.setattr(memory_models, "expand", two)
    c = _use(monkeypatch, FakeClient())
    await _run()
    queries = {kw["searchCriteria"]["searchQuery"] for _, kw in c.calls if "searchCriteria" in kw}
    assert queries == {"where does Ann live", "what does Bob eat"}


@pytest.mark.asyncio
async def test_recall_entity_listing_adds_all_records_for_entity(monkeypatch):
    def handler(op, kw):
        f = kw.get("metadataFilters")
        if op == "list" and not f:
            return [_rec("x", "Ann: a: 1", entity="Ann"), _rec("y", "Bob: a: 1", entity="Bob")]
        if op == "list" and f[0]["right"]["metadataValue"]["stringValue"] == "Ann":
            return [
                _rec("a1", "Ann: city: Rome", kind="claim", entity="Ann"),
                _rec("a2", "Ann: pet: cat", kind="claim", entity="Ann"),
            ]
        return []

    c = _use(monkeypatch, FakeClient(handler))
    page = await memory_recall.recall(memory_id=MEM, namespaces=NS, question="Tell me about ann, not bobby")
    assert sorted(_texts(page)) == ["Ann: city: Rome", "Ann: pet: cat"]
    assert len([kw for o, kw in c.calls if o == "list" and kw.get("metadataFilters")]) == 1
    await memory_recall.recall(memory_id=MEM, namespaces=NS, question="ann again")
    assert len([kw for o, kw in c.calls if o == "list" and not kw.get("metadataFilters")]) == 1  # cached


@pytest.mark.asyncio
async def test_recall_time_window_filters_after_fetch(monkeypatch):
    rows = [_rec("in", "in june", ts="2026-06-10T00:00:00Z"), _rec("out", "in july", ts="2026-07-10T00:00:00Z")]
    _use(monkeypatch, FakeClient(lambda op, kw: rows if kw["namespace"] == NS["turns"] else []))
    page = await memory_recall.recall(memory_id=MEM, namespaces=NS, question="what happened in June 2026?")
    assert _texts(page) == ["in june"]


@pytest.mark.asyncio
async def test_recall_failed_arm_does_not_fail_recall(monkeypatch):
    from botocore.exceptions import ClientError

    def handler(op, kw):
        if kw["namespace"] == NS["facts"]:
            raise ClientError({"Error": {"Code": "Throttling", "Message": "x"}}, "Retrieve")
        return [_rec("t", "still here")]

    _use(monkeypatch, FakeClient(handler))
    assert "still here" in _texts(await _run())


def _ann_handler(rows):
    def handler(op, kw):
        f = kw.get("metadataFilters")
        if op == "list" and not f:
            return [_rec("x", "Ann: a: 1", entity="Ann")]
        return rows if op == "list" else []

    return handler


@pytest.mark.asyncio
async def test_entity_cache_skips_failed_listing(monkeypatch):
    from botocore.exceptions import ClientError

    state = {"fail": True}

    def handler(op, kw):
        if op == "list" and not kw.get("metadataFilters") and state["fail"]:
            raise ClientError({"Error": {"Code": "Throttling", "Message": "x"}}, "List")
        return _ann_handler([_rec("a1", "Ann: city: Rome", kind="claim", entity="Ann")])(op, kw)

    c = _use(monkeypatch, FakeClient(handler))
    await memory_recall.recall(memory_id=MEM, namespaces=NS, question="ann?")
    state["fail"] = False
    page = await memory_recall.recall(memory_id=MEM, namespaces=NS, question="ann?")
    assert _texts(page) == ["Ann: city: Rome"]
    assert len([kw for o, kw in c.calls if o == "list" and not kw.get("metadataFilters")]) == 2


@pytest.mark.asyncio
async def test_stale_entities_answer_while_a_background_read_refreshes(monkeypatch):
    names = {"now": "Ann"}

    def handler(op, kw):
        if op == "list" and not kw.get("metadataFilters"):
            return [_rec("x", "n: a: 1", entity=names["now"])]
        return []

    c = _use(monkeypatch, FakeClient(handler))
    key = (MEM, NS["facts"])
    await _run()  # cold: waits for the listing
    assert memory_recall._entity_cache[key][1] == {"Ann"}
    names["now"] = "Bob"
    memory_recall._entity_cache[key] = (0.0, {"Ann"})  # stale
    assert await memory_recall._entities(c, MEM, NS["facts"]) == {"Ann"}  # the stale list, no wait
    await memory_recall._entity_refresh[key]
    assert memory_recall._entity_cache[key][1] == {"Bob"} and key not in memory_recall._entity_refresh


@pytest.mark.asyncio
async def test_param_validation_error_is_not_swallowed(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    with pytest.raises(Exception, match="memoryId"):
        await memory_recall.recall(memory_id="x", namespaces=NS, question="q?")
    assert c.calls


@pytest.mark.asyncio
async def test_listing_is_capped_in_pages_and_records(monkeypatch):
    pages = []

    def list_memory_records(**kw):
        pages.append(kw)
        return {"memoryRecordSummaries": [_rec(f"r{len(pages)}", "Ann: a: 1", entity="Ann")], "nextToken": "n"}

    client = FakeClient()
    client.list_memory_records = list_memory_records
    assert len(memory_recall._list(client, MEM, NS["facts"], [], 1000)) == memory_recall.MAX_PAGES
    pages.clear()
    assert len(memory_recall._list(client, MEM, NS["facts"], [], 3)) == 3 and len(pages) == 3


@pytest.mark.asyncio
async def test_recall_scores_below_floor_return_only_positive(monkeypatch):
    async def scores(query, documents, top_n):
        return [(0, 0.9), (1, 0.0), (2, 0.0)]

    monkeypatch.setattr(memory_models, "rerank", scores)
    rows = [_rec(f"t{i}", f"note {i}") for i in range(3)]
    _use(monkeypatch, FakeClient(lambda op, kw: rows if kw["namespace"] == NS["turns"] else []))
    assert _texts(await _run()) == ["note 0"]


@pytest.mark.asyncio
async def test_recall_caps_parts_at_five(monkeypatch):
    async def seven(question):
        return [f"part {i}" for i in range(7)]

    monkeypatch.setattr(memory_models, "expand", seven)
    c = _use(monkeypatch, FakeClient())
    await _run()
    assert len({kw["searchCriteria"]["searchQuery"] for _, kw in c.calls if "searchCriteria" in kw}) == 5


@pytest.mark.asyncio
async def test_recall_caps_entities_at_five_per_part(monkeypatch):
    names = [f"Name{i}" for i in range(8)]

    def handler(op, kw):
        listing = op == "list" and not kw.get("metadataFilters")
        return [_rec(n, f"{n}: a: 1", entity=n) for n in names] if listing else []

    c = _use(monkeypatch, FakeClient(handler))
    await memory_recall.recall(memory_id=MEM, namespaces=NS, question=" ".join(names))
    assert len([kw for o, kw in c.calls if o == "list" and kw.get("metadataFilters")]) == 5


@pytest.mark.asyncio
async def test_time_window_never_drops_a_current_claim(monkeypatch):
    # Created long before "today": the claim is still what is true now.
    claim = _rec("c", "Ann: release date: Friday", ts="2026-01-05T00:00:00Z", kind="claim", status="current")
    old_turn = _rec("t", "Ann: shipping today?", ts="2026-01-05T00:00:00Z")
    _use(
        monkeypatch,
        FakeClient(
            lambda op, kw: (
                [claim] if kw["namespace"] == NS["facts"] else [old_turn] if kw["namespace"] == NS["turns"] else []
            )
        ),
    )
    page = await memory_recall.recall(memory_id=MEM, namespaces=NS, question="can we ship today?")
    assert _texts(page) == ["Ann: release date: Friday"]


@pytest.mark.asyncio
async def test_as_of_filters_turns_and_entries_by_creation(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    await _run(as_of=AS_OF)
    before = [
        {
            "left": {"metadataKey": "x-amz-agentcore-memory-createdAt"},
            "operator": "BEFORE",
            "right": {"metadataValue": {"dateTimeValue": "2026-03-01T00:00:00Z"}},
        }
    ]
    for ns in (NS["turns"], NS["entries"]):
        (kw,) = c.where("retrieve", ns)
        assert kw["searchCriteria"]["metadataFilters"] == before
    await _run()
    assert all("metadataFilters" not in kw["searchCriteria"] for kw in c.where("retrieve", NS["turns"])[1:])


def test_as_of_is_sent_as_iso_utc_and_a_naive_time_is_utc():
    assert memory_recall._iso_utc(datetime(2026, 3, 1, 9, 30)) == "2026-03-01T09:30:00Z"
    assert memory_recall._iso_utc(datetime.fromisoformat("2026-03-01T10:30:00+01:00")) == "2026-03-01T09:30:00Z"


@pytest.mark.asyncio
async def test_a_question_page_holds_50_turn_excerpts_beside_claims(monkeypatch):
    filler = "nothing of note here at all " * 40  # ~1,100 characters before the match
    turns = [_rec(f"t{i}", f"Ann: {filler}the invoice number for job {i} was 7731 {filler}") for i in range(60)]
    claims = [_rec(f"c{i}", f"Ann: topic {i}: value {i}", kind="claim") for i in range(60)]

    def handler(op, kw):
        if op != "retrieve":
            return []
        return {NS["turns"]: turns, NS["facts"]: claims}.get(kw["namespace"], [])

    c = _use(monkeypatch, FakeClient(handler))
    page = await memory_recall.recall(
        memory_id=MEM, namespaces=NS, question="what was the invoice number?", as_of=AS_OF
    )
    kinds = [r["kind"] for r in page["results"]]
    turn = next(r for r in page["results"] if r["kind"] == "turn")
    assert turn["content"].startswith("Ann: …") and "invoice number" in turn["content"]
    assert len(turn["content"]) <= len("Ann: ") + 800
    # 50 claims, then turns of ~200 tokens until the next would pass 8,000 (the 50 cap: test_memory_stages)
    assert kinds.count("claim") == 50 and kinds.count("turn") > 30
    used = page["usedEvidenceTokens"]
    assert used == sum(-(-len(r["content"]) // 4) for r in page["results"]) <= 8000 < used + len(turn["content"]) // 4
    # the as-of filter still narrows the turn arm
    (kw,) = c.where("retrieve", NS["turns"])
    assert kw["searchCriteria"]["metadataFilters"][0]["operator"] == "BEFORE"
    roomy = await memory_recall.recall(memory_id=MEM, namespaces=NS, question="invoice number?", budget_tokens=20_000)
    assert [r["kind"] for r in roomy["results"]].count("turn") == 50


async def test_task_records_go_to_links_not_results(monkeypatch):
    def handler(op, kw):
        if kw["namespace"] == NS["tasks"]:
            return [_rec("c1", "T-42 Acme pricing — working · fin", taskId="t42")]
        if kw["namespace"] == NS["turns"]:
            return [_rec("u1", "Sam: Acme wants the sheet", taskId="t42", threadId="room:r1:m1")]
        return []

    _use(monkeypatch, FakeClient(handler))
    page = await _run(tasks=True)
    assert _texts(page) == ["Sam: Acme wants the sheet"]
    assert page["results"][0]["taskId"] == "t42" and page["results"][0]["threadId"] == "room:r1:m1"
    assert page["taskHits"] == ["t42"]


async def test_shared_lane_never_reads_private_namespaces(monkeypatch):
    client = _use(monkeypatch, FakeClient())
    await _run(namespaces=TEAM_ONLY, tasks=True)
    read = {kw["namespace"] for _, kw in client.calls}
    assert read and not any(":" in ns for ns in read)
    assert TEAM_ONLY["tasks"] in read


async def test_private_lane_reads_private_turns_and_tasks(monkeypatch):
    client = _use(monkeypatch, FakeClient())
    await _run(include_private=True, tasks=True)
    read = {kw["namespace"] for _, kw in client.calls}
    assert {NS["private_turns"], NS["private_tasks"], NS["tasks"]} <= read


async def test_without_tasks_no_task_or_private_turn_arm(monkeypatch):
    client = _use(monkeypatch, FakeClient())
    page = await _run(include_private=True)
    read = {kw["namespace"] for _, kw in client.calls}
    assert not read & {NS["tasks"], NS["private_tasks"], NS["private_turns"]}
    assert "taskHits" not in page


ROOM = "3f1c2a8e-1111-4c1e-9d55-7f3a2b1c0e99"


async def test_a_group_chats_scope_is_read_only_when_passed(monkeypatch):
    """Founder ruling (6 Nov 2026): a group chat's memory is recalled in that chat, never workspace-wide."""
    scope = workspace_memory.room_namespaces("own1", ROOM)
    assert scope == {"room": f"/owners/own1:room_{ROOM}/", "room_turns": f"/turns/own1:room_{ROOM}/"}
    client = _use(monkeypatch, FakeClient())
    await _run(namespaces=TEAM_ONLY, tasks=True)
    assert not {kw["namespace"] for _, kw in client.calls} & set(scope.values())
    client = _use(monkeypatch, FakeClient())
    await _run(namespaces={**TEAM_ONLY, **scope}, tasks=True)
    assert set(scope.values()) <= {kw["namespace"] for _, kw in client.calls}


async def test_recall_page_adds_the_scope_only_for_a_group_chat_audience(monkeypatch):
    from core.services import memory_api, room_memory

    monkeypatch.setattr(memory_api.settings, "AGENTCORE_MEMORY_ID", MEM, raising=False)
    monkeypatch.setattr(memory_api, "_sync_in_background", lambda *a: None)
    monkeypatch.setattr(memory_api, "flags_for", AsyncMock(return_value=True))
    monkeypatch.setattr(memory_api.memory_links, "resolve_links", AsyncMock(return_value=[]))
    recall = AsyncMock(return_value={"results": [], "taskHits": []})
    monkeypatch.setattr(memory_api.memory_recall, "recall", recall)
    group = AsyncMock(return_value=ROOM)
    monkeypatch.setattr(room_memory, "group_room_by_id", group)

    await memory_api.recall_page("own1", None, "q?", audience={"kind": "room", "roomId": ROOM})
    assert recall.await_args.kwargs["namespaces"]["room"] == f"/owners/own1:room_{ROOM}/"
    group.return_value = None  # a channel
    await memory_api.recall_page("own1", None, "q?", audience={"kind": "room", "roomId": ROOM})
    assert "room" not in recall.await_args.kwargs["namespaces"]
    await memory_api.recall_page("own1", None, "q?", audience={"kind": "workspace"})
    assert "room" not in recall.await_args.kwargs["namespaces"]


def test_room_actor_refuses_an_unsafe_room_id():
    with pytest.raises(ValueError):
        workspace_memory.room_actor("own1", "../own2")
