"""The dev e2e runner's pure scoring helpers (scripts/multiparty_e2e/runner.py). No network."""

from types import SimpleNamespace

import pytest

from scripts.multiparty_e2e import runner


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
