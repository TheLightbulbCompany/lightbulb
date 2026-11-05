"""Harness tests (spec 07 §8.6): no model calls, no backend."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import agent
import fixture
import run
import score
from fake_backend import FakeBackend

FIXTURE = Path(__file__).parent.parent / "fixtures" / "workspace-v1.json"


def _ws() -> dict:
    return json.loads(FIXTURE.read_text("utf-8"))


def test_fixture_invariants():
    ws = _ws()
    assert ws == json.loads(json.dumps(fixture.build(7))), "committed fixture is fixture.py --seed 7"
    assert Counter(q["category"] for q in ws["questions"]) == fixture.COUNTS
    assert len(ws["tasks"]) == 40 and 550 <= len(ws["messages"]) <= 650
    tasks = {t["taskId"]: t for t in ws["tasks"]}
    assert sum(t["lane"] == "private" for t in tasks.values()) == 8
    assert sum(t["hidden"] for t in tasks.values()) == 3
    assert sum(bool(t["supersedes"]) for t in tasks.values()) == 4
    for q in ws["questions"]:
        # gold: a task and facts to find, or an explicit abstain
        assert (q["goldTaskIds"] and q["goldFacts"]) or (q["expectAbstain"] and not q["goldTaskIds"]), q["id"]
        assert all(t in tasks for t in q["goldTaskIds"] + q["forbiddenTaskIds"])
        if q["category"] == "private":
            assert q["owner"], q["id"]
    tokens = [c["token"] for c in ws["canaries"]]
    assert len(tokens) == 12 == len(set(tokens))
    text = "\n".join(m["text"] for m in ws["messages"])
    assert all(text.count(t) == 1 for t in tokens)


def test_score_leak_detection():
    ws = _ws()
    canary = next(c for c in ws["canaries"] if c["room"] == "dm_xa")
    q = next(q for q in ws["questions"] if q["audience"] == {"kind": "room", "roomId": "room_ops"})
    clean = {
        "id": q["id"],
        "answer": "",
        "abstain": True,
        "cited": [],
        "read": ["Turns:\n- nothing"],
        "reached": [[]],
        "latencies": [0.1],
    }
    planted = {**clean, "read": [f"Turns:\n- Alice: the door code is {canary['token']}"]}
    assert score.summarize([q], [clean], ws["canaries"])["leaks"] == 0
    leaked = score.summarize([q], [planted], ws["canaries"])
    assert leaked["leaks"] == 1
    assert not score.passes(leaked)["leak=0"]
    # the owner reading their own DM canary is allowed
    owner_q = {**q, "audience": {"kind": "person", "userId": "user_xa"}}
    assert score.leaks(owner_q, planted, ws["canaries"]) == []


def test_hop_loop_caps_at_three_with_fake_llm():
    ws = _ws()

    class AlwaysRecall:
        calls = 0

        async def complete_json(self, prompt, schema):
            self.calls += 1
            return {
                "action": "recall",
                "link_id": "",
                "query": "more",
                "answer": "",
                "abstain": False,
                "cited_task_ids": [],
            }

    class CountingBackend(FakeBackend):
        reads = 0

        async def recall(self, *a, **k):
            self.reads += 1
            return await super().recall(*a, **k)

    llm, backend = AlwaysRecall(), CountingBackend(ws)
    res = asyncio.run(agent.run_question(ws["questions"][0], backend, llm))
    assert backend.reads == agent.MAX_HOPS == 3
    assert llm.calls == 3 and len(res["reached"]) == 3


def test_fake_backend_never_serves_people_dms_or_private_to_others():
    ws = _ws()
    backend = FakeBackend(ws)
    for c in ws["canaries"]:
        page = asyncio.run(backend.recall("door code", {"kind": "room", "roomId": "room_ops"}))
        assert c["token"] not in page["text"]
    own = asyncio.run(backend.recall("door code", {"kind": "person", "userId": "user_xa"}))
    dm_xa = next(c for c in ws["canaries"] if c["room"] == "dm_xa")["token"]
    assert dm_xa in own["text"]
    assert all(c["token"] not in own["text"] for c in ws["canaries"] if c["room"] in ("dm_ab", "gdm_ab"))


def test_resume_from_cache(tmp_path):
    ws = _ws()
    first = agent.HeuristicLLM()
    s1 = asyncio.run(run.run(ws, FakeBackend(ws), first, tmp_path, limit=12))
    assert first.calls > 0 and s1["questions"] == 12
    again = agent.HeuristicLLM()
    s2 = asyncio.run(run.run(ws, FakeBackend(ws), again, tmp_path, limit=12))
    assert again.calls == 0
    assert s2 == s1
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 12


def test_lift_reports_points():
    assert score.lift({"taskHit@2": 0.9, "factAccuracy": 0.8}, {"taskHit@2": 0.0, "factAccuracy": 0.5}) == {
        "taskHit@2": 90.0,
        "factAccuracy": 30.0,
    }


def test_oblique_questions_never_name_the_client_or_title():
    ws = _ws()
    tasks = {t["taskId"]: t for t in ws["tasks"]}
    for q in (q for q in ws["questions"] if q["category"] == "oblique"):
        t = tasks[q["goldTaskIds"][0]]
        assert t["client"].lower() not in q["text"].lower(), q["id"]
        assert t["subject"].lower() not in q["text"].lower(), q["id"]


def test_answer_naming_the_superseded_value_is_wrong():
    ws = _ws()
    q = next(q for q in ws["questions"] if q["category"] == "supersession")
    new, old = q["goldFacts"][0], q["staleFacts"][0]
    assert new != old
    assert score.fact_correct(q, {"answer": f"It is {new}."})
    assert not score.fact_correct(q, {"answer": f"It was {old}, now {new}."})
