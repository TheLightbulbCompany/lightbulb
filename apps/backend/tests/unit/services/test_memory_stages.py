from datetime import UTC, datetime

from core.services.memory_stages import (
    Candidate,
    compose,
    count_tokens,
    from_record,
    rrf,
    time_window,
)

T = datetime(2026, 10, 27, tzinfo=UTC)


def cand(id, content, kind="claim", fused=0.0, **kw):
    return Candidate(id=id, kind=kind, content=content, occurred_at=T, source_id=id, fused=fused, **kw)


def test_rrf_k60_matches_hand_computation():
    out = dict(rrf([["a", "b"], ["b", "c"]]))
    assert out["a"] == 1 / 61
    assert out["b"] == 1 / 62 + 1 / 61
    assert out["c"] == 1 / 62
    assert [i for i, _ in rrf([["a", "b"], ["b", "c"]])] == ["b", "a", "c"]
    assert [i for i, _ in rrf([["x"], ["y"]])] == ["x", "y"]  # tie: first appearance


def test_compose_orders_contested_then_current():
    cands = [
        cand("t", "hello there", kind="turn", fused=0.9),
        cand("c1", "Sam: db version: 14", fused=0.1, speaker="Sam"),
        cand("c2", "Ann: db version: 16", fused=0.2, speaker="Ann"),
        cand("k", "Ann: region: eu", fused=0.5, speaker="Ann"),
        cand("e", "unrelated note", kind="entry", fused=0.8),
    ]
    page = compose(cands, 10_000, T)
    assert [r["rank"] for r in page["results"]] == [1, 2, 3, 4, 5]
    assert [r["sources"][0]["sourceId"] for r in page["results"]] == ["c2", "c1", "k", "t", "e"]
    assert [r["status"] for r in page["results"]][:2] == ["contested", "contested"]
    assert page["asOf"] == T.isoformat()


def test_compose_marks_contested_across_namespaces():
    facts = cand("f", "production database version: 14", entity="production database", attribute="version")
    entry = cand("n", "production database version: 16", kind="entry")
    page = compose([facts, entry], 10_000, T)
    assert [r["status"] for r in page["results"]] == ["contested", "contested"]
    assert len(page["results"]) == 2


def test_compose_stops_at_budget():
    cands = [cand(f"c{i}", f"Ann: topic{i} thing: " + "word " * 40, fused=1 - i / 10) for i in range(5)]
    full = compose(cands, 10_000, T)
    budget = full["usedEvidenceTokens"] // 2
    page = compose(cands, budget, T)
    assert 0 < len(page["results"]) < 5
    assert page["usedEvidenceTokens"] <= budget
    assert [r["sources"][0]["sourceId"] for r in page["results"]] == ["c0", "c1", "c2", "c3"][: len(page["results"])]


def test_compose_parses_history_tail():
    text = (
        "Ann: db version: 16 (since 2026-09-01; was db version: 15, Sam, until 2026-09-01; "
        "was db version: 14, Bo, until 2026-06-01)"
    )
    page = compose([cand("h", text, speaker="Ann")], 10_000, T)
    assert page["results"][0]["history"] == [
        {"value": "15", "speaker": "Sam", "occurredAt": "2026-09-01T00:00:00+00:00"},
        {"value": "14", "speaker": "Bo", "occurredAt": "2026-06-01T00:00:00+00:00"},
    ]
    assert page["results"][0]["status"] == "current"
    assert compose([cand("p", "(since oops")], 10_000, T)["results"][0]["history"] == []


def test_time_window_month_and_year():
    start, end = time_window("what did we decide in March 2026?", T)
    assert start == datetime(2026, 3, 1, tzinfo=UTC)
    assert end == datetime(2026, 3, 31, 23, 59, 59, 999999, tzinfo=UTC)
    assert time_window("anything about the database", T) == (None, None)
    assert time_window("in 2025", T)[0] == datetime(2025, 1, 1, tzinfo=UTC)
    assert time_window("last month", T)[0] == datetime(2026, 9, 1, tzinfo=UTC)
    assert time_window("last week", T)[0] == datetime(2026, 10, 19, tzinfo=UTC)
    assert time_window("on 2026-02-03", T)[1].day == 3


def test_from_record_maps_metadata_and_speaker():
    c = from_record(
        {
            "memoryRecordId": "r1",
            "content": {"text": "Ann: db version: 16 (since 2026-09-01)"},
            "timestamp": 1_790_000_000,
            "metadata": {
                "kind": {"stringValue": "claim"},
                "entity": {"stringValue": "db"},
                "attribute": {"stringValue": "version"},
                "validFrom": {"numberValue": 1_790_000_000},
            },
        },
        "entry",
    )
    assert (c.id, c.kind, c.speaker, c.entity, c.attribute, c.status) == (
        "r1",
        "claim",
        "Ann",
        "db",
        "version",
        "current",
    )
    assert c.valid_from == datetime.fromtimestamp(1_790_000_000, UTC)
    assert from_record({"content": {"text": "plain"}}, "entry").kind == "entry"
    assert count_tokens("abcdefgh") == 2
