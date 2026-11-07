"""The multi-party routing bench (scripts/speak_gate_bench/multiparty.py) and the dev e2e runner's
pure scoring helpers (scripts/multiparty_e2e/runner.py). No network: Jev is stubbed."""

import json
from types import SimpleNamespace

import pytest

from scripts.multiparty_e2e import runner
from scripts.speak_gate_bench import multiparty as mp


def _row(**kw):
    row = {
        "id": "X01",
        "category": "C",
        "expected": "SILENT",
        "expected_responder": None,
        "goal_status": "open",
        "people": ["Maya", "Leo"],
        "agents": ["CEO", "Finance", {"name": "Engineering", "listen": "observe"}],
        "transcript": [{"speaker": "Maya", "text": "@leo are you coming?"}],
        "mentions": ["Leo"],
    }
    return row | kw


def test_every_golden_row_loads_and_is_consistent():
    cases = mp.load()
    assert len({c["id"] for c in cases}) == len(cases)
    assert {c["category"] for c in cases} == set(mp.CATEGORIES)
    for c in cases:
        assert (c["expected"] == "SPEAK") == bool(c["expected_responder"])


def test_expand_makes_a_voice_variant_without_handles_or_structured_mentions():
    chat, voice = mp.expand(_row())
    assert chat["mentions"] == ["Leo"] and voice["mentions"] == []
    assert voice["transcript"][0]["text"] == "Leo are you coming?"
    with pytest.raises(ValueError):
        mp.expand(_row(expected="SPEAK"))


def test_deterministic_layer_mirrors_dispatch():
    """The addressing stage's agents (routing v2) join the structured mentions; no stage, no name rule."""
    named = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "Finance, what's runway?"}], mentions=[]))[0]
    assert mp.deterministic(named, mp.build(named), {"agents": ["fin"]})["how"] == "addressed"
    assert mp.deterministic(named, mp.build(named))["how"] == "jev"
    ambient = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "what's runway?"}], mentions=[]))
    chat, voice = (mp.deterministic(c, mp.build(c)) for c in ambient)
    assert chat["candidates"] == ["ceo", "fin"]  # observe never runs in chat
    assert voice["candidates"] == ["ceo", "eng", "fin"]  # Live ignores listen modes
    observe = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "Engineering, is CI red?"}], mentions=[]))
    chat, voice = (mp.deterministic(c, mp.build(c), {"agents": ["eng"]}) for c in observe)
    assert chat["addressed"] == [] and "eng" not in chat["candidates"]  # named, but observe never runs
    assert voice["addressed"] == ["eng"]  # any agent in the room can be asked by voice


def test_agent_chain_at_the_cap_wakes_nobody():
    row = _row(
        agents=["Forge", "Pulse"],
        transcript=[{"speaker": "Maya", "text": "ship it"}, {"speaker": "Forge", "text": "@pulse write the note"}],
        mentions=[],
    )
    below, at = (mp.expand(row | {"agent_hop": hop})[0] for hop in (1, 2))
    assert mp.deterministic(below, mp.build(below))["addressed"] == ["pulse"]
    assert mp.deterministic(at, mp.build(at))["how"] == "capped"


def test_fallback_follows_the_conversation():
    case = mp.expand(
        _row(
            transcript=[{"speaker": "Finance", "text": "Q3 is $1m"}, {"speaker": "Maya", "text": "and Q4?"}],
            mentions=[],
        )
    )[0]
    state = mp.build(case)
    assert mp.fallback(state, ["ceo", "fin"], to_people=False) == ["fin"]  # whoever was already talking
    assert mp.fallback(state, ["ceo", "fin"], to_people=True) == []


async def test_route_applies_jevs_pick(monkeypatch):
    async def decide(meta, message, latest, members, history, **kw):
        return {"who": "fin", "whoP": {"fin": 0.9, "none": 0.1}, "placement": "main"}

    monkeypatch.setattr(mp.jev, "_decide_for_message", decide)
    case = mp.expand(_row(transcript=[{"speaker": "Maya", "text": "what's runway?"}], mentions=[]))[0]
    rec = await mp.route(case)
    assert rec["responders"] == ["Finance"] and rec["how"] == "jev"


def test_score_never_rewards_always_silent():
    recs = [
        {"expected": "SPEAK", "expected_responder": ["Finance"], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": []},
        {"expected": "SILENT", "expected_responder": [], "responders": ["CEO"]},
    ]
    s = mp.score(recs)
    assert s["recall"] == 0 and s["missed"] == 1 and s["false_speak"] == pytest.approx(1 / 3) and s["precision"] == 0


def test_e2e_render_and_hard_checks():
    names = {
        "finance": {"kind": "agent", "id": "a1", "displayName": "Finance", "mentionHandle": "finance"},
        "leo": {"kind": "human", "id": "u1", "displayName": "Leo Test", "mentionHandle": "mp-e2e-leo"},
    }
    text, mentions = runner.render("@{leo} ask {finance}, cc @{finance}", names)
    assert text == "@mp-e2e-leo ask Finance, cc @finance"
    assert mentions == [{"kind": "human", "id": "u1"}, {"kind": "agent", "id": "a1"}]
    step = {
        "expect": {"responder": "finance", "must_not_restate": [0], "must_reference": ["March"], "must_not_say": ["$5"]}
    }
    said = ["Acme renews in March, I checked with their CFO"]
    good = runner.hard_checks(step, [{"role": "finance", "text": "Noted: renewal is March 1st."}], said)
    assert all(good.values())
    bad = runner.hard_checks(step, [{"role": "ceo", "text": "Acme renews in March, checked with their CFO. $5"}], said)
    assert not bad["responder"] and not bad["no_restate_step0"] and not bad["avoids:$5"]


def test_overlap_and_judge_parsing():
    assert runner.overlap("MRR is 42k", "MRR is 42k as of yesterday") == pytest.approx(2 / 3)
    assert runner.overlap("anything", "") == 0
    assert runner.parse_judge('sure: {"pass": true, "reason": "fine"}') == {"pass": True, "reason": "fine"}
    assert runner.parse_judge("not json")["pass"] is False


def test_score_splits_partial_sets_into_missed_and_extras_into_wrong_agent():
    exp = {"expected": "SPEAK", "expected_responder": ["Finance", "Growth"]}
    s = mp.score([exp | {"responders": ["Finance"]}, exp | {"responders": ["Finance", "Growth", "CEO"]}])
    assert s["missed"] == 0.5 and s["wrong_agent"] == 0.5 and s["recall"] == 0


@pytest.mark.parametrize("window", [3, 4, 5])
def test_collect_stops_at_the_race_deadline(monkeypatch, window):
    clock = {"t": 0.0}

    class FakeTime:
        monotonic = staticmethod(lambda: clock["t"])

        @staticmethod
        def sleep(s):
            clock["t"] += s

    def api(method, path, **kw):
        assert kw["timeout"] <= window - clock["t"] + 1e-9  # no request may outlive the window
        clock["t"] += 0.5  # each request costs time
        return {"messages": []} if path.endswith("/messages") else {"activeRuns": []}

    monkeypatch.setattr(runner, "time", FakeTime)
    runner.collect(api, "r", 0, {}, {}, window=window, expect_speak=True, settle={}, start=0.0)
    assert clock["t"] == pytest.approx(window)  # an uncapped 3 s sleep, or a poll past it, overshot


def test_annotate_keeps_goal_status():
    scn = {"steps": [{"say": "q"}, {"say": "a", "expect": {"responder": None, "goal": "answered"}}]}
    res = runner.annotate({"steps": [{"step": 0}, {"step": 1}]}, scn)
    assert res["steps"][1] == {"step": 1, "expected": "none", "goal": "answered"}
    assert res["steps"][0]["goal"] is None


def test_split_at_hands_pre_post_replies_to_the_previous_step():
    before, after = runner.split_at([{"seq": 2}, {"seq": 4}], 3)
    assert before == [{"seq": 2}] and after == [{"seq": 4}]


async def test_stub_cards_list_role_tools():
    card = await mp._stub_card("org", "fin", "Finance")
    assert card.startswith("Finance (role: cfo).") and "Tools: google-sheets, paperclip, stripe." in card
    assert set(mp.STUB_TOOLS) == {role for _, role in mp.PRESETS.values()}


def test_dev_only_accepts_the_dev_host_alone():
    runner.dev_only("https://api-dev.thelightbulb.company/api/v1")
    for url in (
        "https://api-dev.thelightbulb.company.evil/api/v1",
        "https://api-dev.@api.thelightbulb.company/api/v1",
        "http://api-dev.thelightbulb.company/api/v1",
        "https://api.thelightbulb.company/api/v1",
    ):
        with pytest.raises(SystemExit):
            runner.dev_only(url)


def test_play_keeps_the_cursor_and_judges_after_every_post(monkeypatch):
    """A race: no judge call may sit between two posts, and the cursor is not jumped past the post."""
    events, seq = [], {"n": 0}

    def api(method, path, **kw):
        if method == "POST" and path == "/messaging/rooms":
            members = [{"kind": "agent", "id": "a1", "displayName": "Finance", "mentionHandle": "finance"}]
            members += [{"kind": "human", "id": f"u-{k}", "displayName": k.title()} for k in ("maya", "leo")]
            return {"roomId": "r1", "members": members}
        if method == "POST":
            seq["n"] += 1
            events.append(("post", kw["json"]["text"]))
            return {"messageId": f"m{seq['n']}", "messageSeq": seq["n"]}
        return {}

    def collect(api_, room_id, after_seq, *a, **kw):
        events.append(("collect", after_seq))
        return [], seq["n"]

    def judge(transcript, step, replies, key):
        events.append(("judge", transcript[-1]))
        return {"pass": True, "reason": ""}

    monkeypatch.setattr(runner, "roster", lambda api_: [{"displayName": "Finance", "id": "a1"}])
    monkeypatch.setattr(runner, "collect", collect)
    monkeypatch.setattr(runner, "judge", judge)
    scn = {
        "id": "race",
        "cast": ["maya", "leo"],
        "agents": {"finance": "active"},
        "steps": [
            {"as": "maya", "say": "q", "expect": {"responder": None, "judge": "quiet"}},
            {"as": "leo", "after": 3, "say": "a", "expect": {"responder": None, "judge": "quiet"}},
        ],
    }
    api.s = SimpleNamespace(token=lambda: "jwt")
    apis = {"maya": api, "leo": api}
    runner.play(scn, apis, {"maya": "u-maya", "leo": "u-leo"}, "key", keep=True)
    kinds = [k for k, _ in events]
    assert kinds == ["post", "collect", "post", "collect", "judge", "judge"]
    assert [v for k, v in events if k == "collect"] == [0, 1]  # from the last seen seq, not the new post's
    assert [v for k, v in events if k == "judge"] == ["Maya: q", "Leo: a"]


def test_report_only_counts_the_selected_cases(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mp, "RAW", tmp_path / "raw")
    monkeypatch.setattr(mp, "HERE", tmp_path)
    (tmp_path / "results").mkdir()
    (tmp_path / "raw").mkdir()
    rec = {"category": "C", "subtype": None, "cue": "cued", "variant": "chat", "how": "jev", "whoP": None}
    rows = [
        rec | {"id": i, "expected": "SILENT", "expected_responder": [], "responders": []}
        for i in ("A03-chat", "C01-chat")
    ]
    (tmp_path / "raw" / "t-multiparty.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    mp.main(["--report-only", "--tag", "t", "--only", "^C01-chat$"])
    assert "| **total** | 1 |" in capsys.readouterr().out


# ------------------------------------------------------------------ G1: cases from 42 real failures
def test_hard_checks_cap_reply_length_and_accept_any_of_several_references():
    replies = [{"role": "ceo", "text": "We go with HN."}]
    step = {"expect": {"responder": "ceo", "max_chars": 10, "must_reference_any": ["Hacker News", "HN"]}}
    checks = runner.hard_checks(step, replies, [""])
    assert checks["max_chars:10"] is False
    assert checks["references_any"] is True
    step = {"expect": {"responder": "ceo", "must_reference_any": ["Product Hunt"]}}
    assert runner.hard_checks(step, replies, [""])["references_any"] is False


def test_board_checks_unchanged_and_status_by_title():
    before = {"Ship the ChatGPT plugin": "todo", "Ship the Claude plugin": "todo"}
    after = {"Ship the ChatGPT plugin": "cancelled", "Ship the Claude plugin": "todo"}
    exp = {
        "board": {
            "unchanged": True,
            "status": {"Ship the ChatGPT plugin": "cancelled", "Ship the Claude plugin": "todo"},
        }
    }
    checks = runner.board_checks(exp, before, after)
    assert checks == {
        "board_unchanged": False,
        "status:Ship the ChatGPT plugin": True,
        "status:Ship the Claude plugin": True,
    }
    assert runner.board_checks({"board": {"status": {"Gone": "todo"}}}, before, after) == {"status:Gone": False}
    assert runner.board_checks({}, before, after) == {}


def test_g1_scenarios_load_and_every_case_is_scored():
    scns = runner.load_scenarios([str(runner.HERE / "scenarios" / "G1.yaml")], "")
    assert len(scns) >= 14 and len({s["id"] for s in scns}) == len(scns)
    for scn in scns:
        assert scn["source"].startswith("G1#"), scn["id"]
        scored = [s for s in scn["steps"] if s.get("expect")]
        assert len(scored) == 1 and scored[0]["expect"].get("judge"), scn["id"]
        assert set(scn.get("seed") or {}) <= {"tasks"}, scn["id"]


def test_play_seeds_tasks_checks_the_board_and_archives_every_new_task(monkeypatch):
    issues, archived = {"old": {"id": "old", "title": "Existing", "status": "todo"}}, []

    def api(method, path, **kw):
        if method == "POST" and path == "/messaging/rooms":
            return {
                "roomId": "r1",
                "members": [
                    {"kind": "agent", "id": "a1", "displayName": "CEO", "mentionHandle": "ceo"},
                    {"kind": "human", "id": "u-maya", "displayName": "Maya"},
                ],
            }
        if method == "GET" and path == "/teams/issues":
            return {"issues": list(issues.values())}
        if method == "POST" and path == "/teams/issues":
            new = {"id": f"i{len(issues)}", **kw["json"]}
            issues[new["id"]] = new
            return new
        if method == "POST" and path.endswith("/archive"):
            archived.append(path.split("/")[3])
            return {}
        if method == "POST":  # the message: the agent "files a task" while answering
            issues["agent"] = {"id": "agent", "title": "Filed by the agent", "status": "todo"}
            return {"messageId": "m1", "messageSeq": 1}
        return {}

    monkeypatch.setattr(runner, "roster", lambda api_: [{"displayName": "CEO", "id": "a1"}])
    monkeypatch.setattr(
        runner, "collect", lambda *a, **kw: ([{"name": "CEO", "role": "ceo", "text": "ok", "seq": 2}], 2)
    )
    scn = {
        "id": "g",
        "cast": ["maya"],
        "agents": {"ceo": "mentions"},
        "seed": {"tasks": [{"title": "Seeded", "status": "todo"}]},
        "steps": [{"as": "maya", "say": "@{ceo} hi", "expect": {"responder": "ceo", "board": {"unchanged": True}}}],
    }
    api.s = SimpleNamespace(token=lambda: "jwt")
    res = runner.play(scn, {"maya": api}, {"maya": "u-maya"}, None, keep=True)
    assert res["steps"][0]["checks"]["board_unchanged"] is False  # the agent filed a task
    assert sorted(archived) == ["agent", "i1"]  # the seeded task and the agent's, never the existing one


def test_open_tasks_template_is_the_boards_real_open_count():
    rows = {
        "a": {"title": "A", "status": "todo"},
        "b": {"title": "B", "status": "done"},
        "c": {"title": "C", "status": "blocked"},
    }
    step = {"say": "x", "expect": {"must_reference_any": ["{open_tasks}"], "judge": "states {open_tasks}"}}
    filled = runner.fill_board_counts(step, rows)
    assert filled["expect"] == {"must_reference_any": ["2"], "judge": "states 2"}
    assert step["expect"]["judge"] == "states {open_tasks}"  # the scenario itself is not mutated


def test_a_scenario_that_errors_is_a_row_in_the_summary_not_the_end_of_the_run():
    table = runner.summarize([{"id": "G1-x", "error": "POST /teams/issues -> 422", "steps": []}])
    assert "| G1-x | – | – | – | ERROR POST /teams/issues -> 422 | – |" in table
