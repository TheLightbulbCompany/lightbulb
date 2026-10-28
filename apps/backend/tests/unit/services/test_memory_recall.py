"""Recall: expand, search AgentCore several ways, fuse, rerank, compose -- against a recording client
whose every call is validated against the real bedrock-agentcore request shape."""

from datetime import UTC, datetime

import boto3
import pytest
from botocore.validate import validate_parameters

from core.services import memory_models, memory_recall, workspace_memory

MODEL = boto3.client("bedrock-agentcore", region_name="us-east-1").meta.service_model
NS = workspace_memory.namespaces_for("own1", "user_a")
TEAM_ONLY = workspace_memory.namespaces_for("own1", None)
MEM = "mem-1234567890"
AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def _meta(**kw):
    return {k: ({"numberValue": v} if isinstance(v, (int, float)) else {"stringValue": v}) for k, v in kw.items()}


def _rec(rid, text, ts="2026-02-01T00:00:00Z", **meta):
    return {
        "memoryRecordId": rid,
        "content": {"text": text},
        "timestamp": datetime.fromisoformat(ts),
        "metadata": _meta(**meta),
    }


class FakeClient:
    """``handler(op, kwargs) -> list[summary]``; records every validated call."""

    def __init__(self, handler=lambda op, kw: []):
        self.handler, self.calls = handler, []

    def _do(self, op, shape, kw):
        validate_parameters(kw, MODEL.operation_model(shape).input_shape)
        self.calls.append((op, kw))
        return {"memoryRecordSummaries": self.handler(op, kw)}

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
        "turns": "/owners/own1/turns/",
        "entries": "/owners/own1/entries/",
        "private": "/owners/own1:user_a/",
    }
    assert "private" not in TEAM_ONLY


@pytest.mark.asyncio
async def test_recall_empty_namespace_returns_empty_page(monkeypatch):
    _use(monkeypatch, FakeClient())
    page = await _run(as_of=AS_OF)
    assert page["results"] == [] and page["asOf"] == AS_OF.isoformat()


@pytest.mark.asyncio
async def test_recall_only_queries_own_namespaces(monkeypatch):
    c = _use(monkeypatch, FakeClient())
    await _run(namespaces=TEAM_ONLY, include_private=True)
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
    sent = [kw["searchCriteria"]["metadataFilters"] for kw in c.where("retrieve", NS["facts"])]
    for status in ("current", "contested"):
        s = {
            "left": {"metadataKey": "status"},
            "operator": "EQUALS_TO",
            "right": {"metadataValue": {"stringValue": status}},
        }
        assert [s, start, open_ended] in sent and [s, start, later] in sent
    assert len(sent) == 4


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
