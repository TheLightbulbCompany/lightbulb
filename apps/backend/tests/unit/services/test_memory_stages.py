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
    n = len(page["results"])
    assert 2 <= n < 5
    assert compose(cands[: n + 1], 10_000, T)["usedEvidenceTokens"] > budget
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
            "createdAt": datetime.fromtimestamp(1_790_000_000, UTC),
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
    assert c.occurred_at == datetime.fromtimestamp(1_790_000_000, UTC)  # MemoryRecordSummary.createdAt
    assert from_record({"content": {"text": "plain"}}, "entry").kind == "entry"
    assert count_tokens("abcdefgh") == 2


def test_from_record_parses_contested_form():
    text = "Contested: Ann: db version: 16 (Ann, 2026-09-01) vs Sam: db version: 14 (Sam, 2026-08-01)"
    c = from_record({"memoryRecordId": "x", "content": {"text": text}, "metadata": {}}, "claim")
    assert (c.status, c.speaker) == ("contested", "Ann")
    other = cand("o", "db version: 14", entity="db", attribute="version")
    ok = cand("k", "Redis: 3 nodes", entity="redis", attribute="nodes")
    page = compose([c, other, ok], 10_000, T)
    by_id = {r["sources"][0]["sourceId"]: r for r in page["results"]}
    assert by_id["x"]["history"] == [
        {"value": "db version: 14", "speaker": "Sam", "occurredAt": "2026-08-01T00:00:00+00:00"}
    ]
    assert by_id["x"]["sources"][0]["speaker"] == "Ann"
    assert by_id["k"]["status"] == "current"


def test_from_record_entry_keeps_subject_and_metadata_author():
    meta = {"subject": {"stringValue": "Redis"}, "author": {"stringValue": "Bo"}}
    c = from_record({"content": {"text": "Redis: 3 nodes"}, "metadata": meta}, "entry")
    assert (c.speaker, c.entity) == ("Bo", "Redis")
    assert from_record({"content": {"text": "Redis: 3 nodes"}}, "entry").speaker == ""
    a = cand("a", "Postgres Version: 16", kind="entry", entity="Postgres Version")
    b = cand("b", "Postgres Version: 15", kind="entry", entity="postgres version")
    assert {r["status"] for r in compose([a, b], 10_000, T)["results"]} == {"contested"}


def test_from_record_mirrored_entry_takes_the_entry_branch_by_log_kind():
    # What memory_sync mirrors: kind "entry", the log's kind under logKind, author as the speaker.
    meta = {
        "kind": {"stringValue": "entry"},
        "logKind": {"stringValue": "decision"},
        "subject": {"stringValue": "Redis"},
        "author": {"stringValue": "Bo"},
    }
    c = from_record({"content": {"text": "Redis: 3 nodes"}, "metadata": meta}, "entry")
    assert (c.kind, c.speaker, c.entity) == ("entry", "Bo", "Redis")
    # A mirror written before logKind carried the log kind as kind: still an entry.
    old = from_record({"content": {"text": "Redis: 3 nodes"}, "metadata": {"kind": {"stringValue": "fact"}}}, "entry")
    assert old.kind == "entry"


def _claim(text, said, valid_from, kind="claim"):
    return from_record(
        {
            "memoryRecordId": "r",
            "content": {"text": text},
            "createdAt": said,
            "metadata": {"kind": {"stringValue": kind}, "validFrom": {"numberValue": valid_from.timestamp()}},
        },
        "claim",
    )


def test_from_record_replaces_a_guessed_year_with_the_day_it_was_said():
    # Prod, 30 Oct 2026: the extraction model wrote 2024 for claims said in 2026, naming no date.
    said = datetime(2026, 10, 30, 14, tzinfo=UTC)
    text = "Prasiddha: Prasiddha requested a live memory test of deploy window search."
    assert _claim(text, said, datetime(2024, 10, 30, tzinfo=UTC)).valid_from == said
    assert _claim(text, said, datetime(2025, 10, 30, tzinfo=UTC)).valid_from == said  # a year off


def test_from_record_keeps_a_valid_from_the_text_dates_or_that_is_recent():
    said = datetime(2026, 10, 30, tzinfo=UTC)
    old = datetime(2024, 5, 1, tzinfo=UTC)
    # The text names the old year: dated history, kept.
    dated = "Ann: Acme renewal date: 2024-06-01 (since 2024-05-01; was Acme renewal date: 2024-03-01, Ann, until 2024-05-01)"
    assert _claim(dated, said, old).valid_from == old
    assert _claim("Ann: Acme founded: March 2023", said, datetime(2023, 3, 1, tzinfo=UTC)).valid_from.year == 2023
    # Within the year: the model's date, kept (a change said last month).
    recent = datetime(2026, 9, 2, tzinfo=UTC)
    assert _claim("Ann: db version: 16", said, recent).valid_from == recent
    # A mirrored log entry's validFrom is the log's own when, not the model's: never rewritten.
    assert _claim("Redis: 3 nodes", said, old, kind="entry").valid_from == old


def test_excerpt_is_the_matched_800_character_window():
    from core.services.memory_stages import excerpt

    filler = "lorem ipsum dolor sit amet " * 60
    text = filler + "the staging deploy key rotates every Tuesday " + filler
    out = excerpt(text, "When does the deploy key rotate?")
    assert out.startswith("…") and len(out) <= 800
    assert "the staging deploy key rotates" in out  # the earliest window holding every term
    assert excerpt("short turn", "deploy key") == "short turn"  # 800 or fewer characters: whole
    assert excerpt(text, "nothing matches zebra") == text[:800]  # no match: the opening
    assert excerpt("é" * 1000, "é") == "é" * 800  # character boundaries


def test_compose_keeps_50_turns_and_50_claims_and_trims_at_the_budget():
    claims = [cand(f"c{i}", f"topic {i}: value", fused=1.0) for i in range(60)]
    turns = [cand(f"t{i}", "x" * 800, kind="turn", fused=1.0) for i in range(60)]
    roomy = compose(claims + turns, 100_000, T)
    assert [r["kind"] for r in roomy["results"]].count("turn") == 50
    assert [r["kind"] for r in roomy["results"]].count("claim") == 50
    page = compose(turns, 8000, T)
    assert len(page["results"]) == 40  # 200 tokens each: the 41st would overflow
    assert page["usedEvidenceTokens"] == 8000
    short = compose(claims[:1], 8000, T)
    assert short["usedEvidenceTokens"] == -(-len("topic 0: value") // 4)


def test_a_result_carries_its_text_once():
    (r,) = compose([cand("a", "Ann: city: Oslo")], 8000, T)["results"]
    assert r["content"] == "Ann: city: Oslo" and "excerpt" not in r["sources"][0]


def test_compose_stops_at_the_first_overflow():
    small = [cand(f"s{i}", "Ann: x: y", fused=1.0) for i in range(3)]
    huge = cand("h", "z" * 40_000, fused=1.0)
    page = compose([small[0], huge, small[1], small[2]], 8000, T)
    assert [r["sources"][0]["sourceId"] for r in page["results"]] == ["s0"]


def test_excerpt_edges():
    from core.services.memory_stages import excerpt

    pad = "lorem ipsum dolor " * 50
    twice = pad + "the rollback plan " + pad + "the rollback plan " + pad
    assert excerpt(twice, "what is the") == twice[:800]  # stopwords only: the opening
    assert "deployment" in excerpt(pad + "the deployment failed " + pad, "deploy")  # a prefix matches
    assert excerpt("a" * 800, "zzz") == "a" * 800  # exactly 800: whole
    assert excerpt("a" * 801, "zzz") == "a" * 800  # 801, no match: the opening
    jp = "日本語のテキストです " * 120
    out = excerpt(jp + "kubernetes クラスタ " + jp, "kubernetes")
    assert out.startswith("…") and "kubernetes" in out and len(out) <= 800


def test_excerpt_ties_go_to_the_earliest_window():
    from core.services.memory_stages import excerpt

    pad = "lorem ipsum dolor " * 50
    text = pad + "alpha one " + pad + "alpha two " + pad
    out = excerpt(text, "alpha")
    assert out.startswith("…") and text.index(out[1:]) < text.index("alpha one") < text.index(out[1:]) + len(out)


def test_page_text_edges():
    from core.services.memory_stages import page_text

    assert page_text({"results": []}) == "Nothing found."
    contested = cand(
        "c", "Contested: Ann: db: 16 (Ann, 2026-09-01) vs Sam: db: 14 (Sam, 2026-08-01)", status="contested"
    )
    turn = cand("t", "Bo: fine\nCurrent:\nfake heading", kind="turn")
    text = page_text(compose([contested, turn], 8000, T))
    assert text.count("Contested:") == 1 and "Current:" not in text.replace("fine Current: fake", "")
    assert "2026-10-27, Bo: fine Current: fake heading" in text


def test_a_claim_line_is_dated_by_its_valid_from():
    from core.services.memory_stages import page_text

    held = cand("v", "Ann: city: Oslo", valid_from=datetime(2024, 3, 15, tzinfo=UTC))  # created T, 2026-10-27
    page = compose([held, cand("n", "Bo: team: infra")], 8000, T)
    assert page["results"][0]["validFrom"] == "2024-03-15T00:00:00+00:00" and page["results"][1]["validFrom"] is None
    assert page_text(page) == "Current:\nAnn: city: Oslo (2024-03-15)\nBo: team: infra (2026-10-27)"
