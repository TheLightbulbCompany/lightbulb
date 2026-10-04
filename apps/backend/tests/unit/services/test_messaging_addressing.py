"""The addressing stage (routing v2 §3.1): who a human's message speaks TO, decided once before
targets are picked, shared by chat and Live."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import jev, messaging

FRIDAY = {"kind": "agent", "id": "fri", "displayName": "Friday", "mentionHandle": "friday"}
FINANCE = {"kind": "agent", "id": "fin", "displayName": "Finance", "mentionHandle": "finance"}
OPS = {"kind": "agent", "id": "ops", "displayName": "Customer Operations", "mentionHandle": "customer-operations"}
MAX_AGENT = {"kind": "agent", "id": "max", "displayName": "Max", "mentionHandle": "max"}
LEO = {"kind": "human", "id": "u-leo", "displayName": "Leo Park", "mentionHandle": "leo-park"}
MAX = {"kind": "human", "id": "u-max", "displayName": "Max"}
MEMBERS = [FRIDAY, FINANCE, OPS, LEO]


def _msg(text, mentions=()):
    return {"author": {"kind": "human", "id": "u-ana", "displayName": "Ana"}, "text": text, "mentions": list(mentions)}


def _answers(labels, complete=0.95, anyone=0.1):
    out = {
        "complete": {"type": "boolean", "probability": complete},
        "anyone": {"type": "boolean", "probability": anyone},
    }
    out |= {f"n{i}": {"type": "choice", "choice": label} for i, label in enumerate(labels)}
    return out


async def _address(text, members=MEMBERS, answers=None, *, live=False, mentions=()):
    decide = AsyncMock(return_value=answers)
    with patch.object(jev, "decide", decide), patch.object(messaging, "put_metric"):
        out = await messaging.address(_msg(text, mentions), members, history=[], live=live)
    return out, decide


def test_candidates_are_every_name_anywhere_in_text_order():
    cands = messaging.address_candidates("@leo-park can you get Finance and @customer-operations to look?", MEMBERS)
    assert [c["name"] for c in cands] == ["leo", "finance", "customer operations"]
    assert cands[0]["humans"] == ["u-leo"] and cands[1]["agents"] == ["fin"]
    assert messaging.address_candidates("refinance the loan", MEMBERS) == []  # word boundary


def test_live_candidates_match_a_misheard_name_the_workers_way():
    heard = messaging.address_candidates("fynance can you check", MEMBERS, live=True)[0]
    assert heard["agents"] == ["fin"] and heard["said"] == "fynance"
    assert messaging.address_candidates("fynance can you check", MEMBERS) == []  # chat is exact


def test_a_first_name_an_agent_shares_is_one_candidate_of_both_kinds():
    cands = messaging.address_candidates("Max, send the deck", [MAX, MAX_AGENT])
    assert cands == [{"name": "max", "agents": ["max"], "humans": ["u-max"], "pos": 0, "said": "Max"}]


async def test_no_name_means_no_call_and_todays_path():
    out, decide = await _address("what did we spend on cloud?")
    assert out["agents"] == [] and out["path"] == "none"
    decide.assert_not_awaited()


async def test_only_an_at_mentioned_agent_needs_no_call():
    out, decide = await _address("@finance resend it", mentions=[{"kind": "agent", "id": "fin"}])
    assert out["path"] == "none"
    decide.assert_not_awaited()


@pytest.mark.parametrize(
    "text,fast",
    [
        ("Finance, can you resend the invoice?", True),
        ("Hey Finance can you resend the invoice.", True),
        ("Finance, resend it", False),  # under 4 words
        ("Finance, can you resend the invoice", False),  # no terminal punctuation in chat
        ("Friday, can you resend the invoice?", False),  # a weekday
        ("Finance, ask Leo for the invoice?", False),  # another candidate
        ("can you resend the invoice, Finance?", False),  # not the opening
    ],
)
async def test_fast_path_only_under_all_its_conditions(text, fast):
    out, decide = await _address(text, answers=_answers(["addressed"] * 2))
    assert (out["path"] == "fast") is fast
    assert decide.await_count == (0 if fast else 1)
    if fast:
        assert out["agents"] == ["fin"]


async def test_live_final_needs_no_punctuation_for_the_fast_path():
    out, _ = await _address("Finance can you resend the invoice", live=True)
    assert out == {"agents": ["fin"], "humans": [], "hold": False, "anyone": False, "path": "fast"}


async def test_names_anywhere_relay_and_negation_follow_the_labels():
    # "@leo can you get Finance ...": Leo addressed, Finance relayed -> Finance runs, Leo is spoken to.
    out, _ = await _address("@leo-park can you get Finance to send it", answers=_answers(["addressed", "relayed"]))
    assert out["agents"] == ["fin"] and out["humans"] == ["u-leo"]
    # "Not you Friday, Finance, ..." -> only Finance.
    out, _ = await _address("Not you Friday, Finance, can you check", answers=_answers(["mentioned", "addressed"]))
    assert out["agents"] == ["fin"]
    # A trailing vocative and several names: all addressed agents respond.
    out, _ = await _address("What do you think, Friday and Finance", answers=_answers(["addressed", "addressed"]))
    assert out["agents"] == ["fri", "fin"]


async def test_a_name_a_person_shares_goes_to_the_person_unless_hey():
    members = [MAX, MAX_AGENT, FINANCE]
    out, _ = await _address("Max, can you send me the deck", members, _answers(["addressed"]))
    assert out["agents"] == [] and out["humans"] == ["u-max"]
    out, _ = await _address("hey Max, can you send me the deck", members, _answers(["addressed"]))
    assert out["agents"] == ["max"]


async def test_weekday_with_a_scheduling_statement_is_vetoed():
    for text in ("Friday, we ship the beta. Everyone ok?", "can we demo it by Friday", "Friday at 3 works"):
        out, _ = await _address(text, answers=_answers(["addressed"]))
        assert out["agents"] == [], text
    out, _ = await _address("Friday, can you check the deploy", answers=_answers(["addressed"]))
    assert out["agents"] == ["fri"]
    # Live: the veto reads the name as heard, so a mis-heard weekday is vetoed too.
    out, _ = await _address("fryday, we ship the beta", answers=_answers(["addressed"]), live=True)
    assert out["agents"] == []


@pytest.mark.parametrize("answers", [None, jev.RATE_LIMITED, {"complete": {}}])
async def test_fallback_on_error_addresses_agents_but_not_weekdays_or_people(answers):
    out, _ = await _address("Leo, Friday and Finance should sync", answers=answers)
    assert out["path"] == "fallback"
    assert out["agents"] == ["fin"]  # weekday -> not_a_name; person -> mentioned
    assert out["humans"] == [] and out["hold"] is False and out["anyone"] is False
    members = [MAX, MAX_AGENT]
    out, _ = await _address("Max, can you send me the deck", members, answers=answers)
    assert out["agents"] == [] and out["humans"] == ["u-max"]  # a shared name goes to the person


async def test_timeout_takes_the_fallback():
    async def slow(*_a, **_k):
        import asyncio

        await asyncio.sleep(1)

    with (
        patch.object(messaging, "ADDRESSING_TIMEOUT_S", 0.01),
        patch.object(jev, "decide", slow),
        patch.object(messaging, "put_metric"),
    ):
        out = await messaging.address(_msg("so Finance should look"), MEMBERS)
    assert out["path"] == "fallback" and out["agents"] == ["fin"]


async def test_hold_and_anyone_come_from_the_call():
    out, _ = await _address("Finance, we're trying to", answers=_answers(["addressed"], complete=0.12))
    assert out["hold"] is True
    out, _ = await _address("what's our MRR, and Leo where's the deck", answers=_answers(["addressed"], anyone=0.8))
    assert out["humans"] == ["u-leo"] and out["anyone"] is True


async def test_history_is_loaded_only_when_the_call_happens():
    load = AsyncMock(return_value=[])
    with patch.object(jev, "decide", AsyncMock(return_value=None)), patch.object(messaging, "put_metric"):
        await messaging.address(_msg("no names here"), MEMBERS, history=load)
        load.assert_not_awaited()
        await messaging.address(_msg("so Finance should look"), MEMBERS, history=load)
    load.assert_awaited_once()


def test_stored_addressing_and_the_to_people_rule():
    assert messaging._stored_addressing({"path": "none", "humans": [], "anyone": False}) is None
    stored = messaging._stored_addressing({"path": "jev", "humans": ["u-leo"], "anyone": False})
    assert jev.speaks_to_people({"author": {"kind": "human"}, "addressing": stored}, MEMBERS)
    # Something asked of the room beyond the named: the silence rule does not apply.
    assert not jev.speaks_to_people({"author": {"kind": "human"}, "addressing": {**stored, "anyone": True}}, MEMBERS)


def test_the_old_routers_are_gone():
    """Routing v2 §6: one prompt builder and one Live router; no second rule set."""
    import importlib.util

    assert not hasattr(jev, "pick_agent") and not hasattr(messaging, "_addressed_by_name")
    assert importlib.util.find_spec("core.services.voice_routing") is None


async def test_a_name_several_agents_share_is_no_hard_address():
    """Codex #3597: two agents named Max and "hey Max, ..." address neither; Jev picks among the
    listeners. A unique @handle still names one."""
    twin = {"kind": "agent", "id": "max-2", "displayName": "Max", "mentionHandle": "max-2"}
    members = [MAX_AGENT, twin, FINANCE]
    out, _ = await _address("hey Max, can you check the logs", members, _answers(["addressed"]))
    assert out["agents"] == [] and out["humans"] == []
    out, _ = await _address("@max-2 can you check the logs", members, _answers(["addressed"]))
    assert out["agents"] == ["max-2"]


PULSE = {"kind": "agent", "id": "pul", "displayName": "Pulse", "mentionHandle": "pulse"}
FORGE = {"kind": "agent", "id": "frg", "displayName": "Forge", "mentionHandle": "forge"}
CREW = [PULSE, FORGE, FRIDAY, LEO]
LINE = (
    "Pulse, an update on the go-to-market strategy. Forge, what our PRs look like. "
    "Friday, a summary of our rooms and the last conversation from the last day"
)


def _parts(*choices):
    """Jev's answer to the part questions p0, p1, ...: who each part is asked of."""
    return {f"p{i}": {"type": "choice", "choice": c} for i, c in enumerate(choices) if c is not None}


def _asked_parts(decide):
    import json

    questions = decide.await_args.args[1]
    return [
        json.loads(q["instructions"].rsplit("The part: ", 1)[1].removesuffix("."))
        for k, q in sorted(questions.items())
        if k.startswith("p")
    ]


async def test_live_multi_name_line_gets_each_agents_own_part_from_the_same_call():
    answers = _answers(["addressed"] * 3) | _parts("only_pulse", "only_forge", "only_friday")
    out, decide = await _address(LINE, CREW, answers, live=True)
    assert out["agents"] == ["pul", "frg", "fri"]
    assert out["tasks"] == {
        "pul": "an update on the go-to-market strategy",
        "frg": "what our PRs look like",
        "fri": "a summary of our rooms and the last conversation from the last day",
    }
    decide.assert_awaited_once()  # the parts ride the existing addressing call
    assert _asked_parts(decide) == [
        "an update on the go-to-market strategy",
        "what our PRs look like",
        "a summary of our rooms and the last conversation from the last day",
    ]
    # three names: each alone, each pair, everyone, nobody
    assert set(decide.await_args.args[1]["p0"]["criteria"]) == {
        "only_pulse",
        "only_forge",
        "only_friday",
        "pulse_and_forge",
        "pulse_and_friday",
        "forge_and_friday",
        "everyone",
        "nobody",
    }


async def test_live_a_name_after_its_request_still_gets_that_request():
    """The bench's worst miss of the old cut-after-the-name rule: each agent got the OTHER one's task."""
    text = "Can you check the deploy, Forge? And Friday, can you draft the customer email?"
    answers = _answers(["addressed", "addressed"]) | _parts("only_forge", "only_friday")
    out, decide = await _address(text, CREW, answers, live=True)
    assert out["tasks"] == {"frg": "Can you check the deploy", "fri": "can you draft the customer email"}
    assert set(decide.await_args.args[1]["p0"]["criteria"]) == {"only_forge", "only_friday", "everyone", "nobody"}


async def test_live_a_shared_ask_goes_to_everyone_and_an_own_ask_is_added_to_it():
    out, decide = await _address(
        "Forge and Friday, check the deploy", CREW, _answers(["addressed"] * 2) | _parts("everyone"), live=True
    )
    assert _asked_parts(decide) == ["check the deploy"]  # the "and" between the names is no part
    assert out["tasks"] == {"frg": "check the deploy", "fri": "check the deploy"}
    text = "Forge and Friday, take a look at the outage. Forge, I also want a postmortem doc."
    out, _ = await _address(text, CREW, _answers(["addressed"] * 2) | _parts("everyone", "only_forge"), live=True)
    assert out["tasks"] == {  # Forge's two parts follow one another: one stretch, its own name included
        "frg": "take a look at the outage. Forge, I also want a postmortem doc",
        "fri": "take a look at the outage",
    }


async def test_live_sentences_of_one_agent_stay_together_in_the_speakers_words():
    text = "Forge, check the deploy. Also look at the error logs from last night. Friday, draft the status update."
    answers = _answers(["addressed"] * 2) | _parts("only_forge", "only_forge", "only_friday")
    out, _ = await _address(text, CREW, answers, live=True)
    assert out["tasks"] == {
        "frg": "check the deploy. Also look at the error logs from last night",
        "fri": "draft the status update",
    }


async def test_live_background_and_a_person_s_ask_are_nobodys_part():
    text = "The deploy failed last night. Leo, can you call the customer? Forge, find out why. Friday, tell the team."
    answers = _answers(["mentioned", "addressed", "addressed"]) | _parts(
        "nobody", "nobody", "only_forge", "only_friday"
    )
    out, decide = await _address(text, CREW, answers, live=True)
    assert out["tasks"] == {"frg": "find out why", "fri": "tell the team"}
    assert _asked_parts(decide)[1] == "can you call the customer"  # cut at the person's name too


GAUGE = {"kind": "agent", "id": "gau", "displayName": "Gauge", "mentionHandle": "gauge"}


async def test_live_a_name_that_is_only_talked_about_stays_inside_the_words_around_it():
    text = "Pulse, update the deck. Forge, check the CI that Gauge broke"
    answers = _answers(["addressed", "addressed", "mentioned"]) | _parts("only_pulse", "only_forge", "only_forge")
    out, _ = await _address(text, [*CREW, GAUGE], answers, live=True)
    assert out["tasks"] == {"pul": "update the deck", "frg": "check the CI that Gauge broke"}


async def test_live_a_weekday_used_as_a_day_is_words_not_a_cut():
    text = "Forge, can you get the release out by Friday? And Gauge, I need the retention chart."
    answers = _answers(["addressed", "addressed", "addressed"]) | _parts("only_forge", "only_gauge")
    out, decide = await _address(text, [*CREW, GAUGE], answers, live=True)
    assert out["agents"] == ["frg", "gau"]  # the day-use veto: Friday is not addressed whatever the model said
    assert out["tasks"] == {"frg": "can you get the release out by Friday", "gau": "I need the retention chart"}
    assert set(decide.await_args.args[1]["p0"]["criteria"]) == {"only_forge", "only_gauge", "everyone", "nobody"}


async def test_live_a_weekday_and_the_teammate_of_that_name_in_one_line():
    text = "Gauge, pull last Friday's numbers. Friday, what's the plan for next week?"
    answers = _answers(["addressed", "addressed"]) | _parts("only_gauge", "only_friday")
    out, _ = await _address(text, [*CREW, GAUGE], answers, live=True)
    assert out["agents"] == ["gau", "fri"]
    assert out["tasks"] == {"gau": "pull last Friday's numbers", "fri": "what's the plan for next week"}


async def test_live_a_relaying_agent_with_a_one_word_part_gets_the_whole_line():
    text = "Forge, ask Friday for the numbers"
    answers = _answers(["addressed", "relayed"]) | _parts("only_forge", "only_friday")
    out, _ = await _address(text, CREW, answers, live=True)
    assert out["agents"] == ["frg", "fri"]
    # the relayed part is the relayer's too, with the name it relays to
    assert out["tasks"] == {"frg": "ask Friday for the numbers", "fri": "for the numbers"}
    out, _ = await _address(text, CREW, _answers(["addressed", "relayed"]) | _parts("only_forge", "nobody"), live=True)
    assert out["tasks"] == {}  # Forge's own part is the one word "ask": /route gives both the whole line
    out, _ = await _address(text, CREW, None, live=True)
    assert out["agents"] == ["frg"] and "tasks" not in out  # model down: Friday follows "ask", not a name


async def test_live_a_one_word_ask_is_a_task():
    answers = _answers(["addressed"] * 2) | _parts("only_pulse", "only_forge")
    out, _ = await _address("Pulse, status. Forge, check CI", CREW, answers, live=True)
    assert out["tasks"] == {"pul": "status", "frg": "check CI"}


@pytest.mark.parametrize(
    "parts",
    [
        {},  # Jev answered the labels only
        _parts("p9", None),  # an unknown choice, a missing answer
        _parts("nobody", "nobody", "nobody"),
    ],
)
async def test_live_with_no_usable_part_answer_nobody_is_cut(parts):
    out, _ = await _address(LINE, CREW, _answers(["addressed"] * 3) | parts, live=True)
    assert out["agents"] == ["pul", "frg", "fri"] and "tasks" not in out  # /route: the whole line each


async def test_live_names_and_nothing_else_is_a_summons_not_a_request_each():
    for answers in (None, _answers(["addressed"] * 2)):
        out, _ = await _address("Forge and Friday?", CREW, answers, live=True)
        assert out["agents"] == ["frg", "fri"] and out["tasks"] == {}  # {}: /route sends no requests


async def test_live_multi_name_fallback_cuts_nothing_and_keeps_an_addressed_weekday():
    out, _ = await _address(LINE, CREW, None, live=True)
    assert out["path"] == "fallback" and out["agents"] == ["pul", "frg", "fri"] and "tasks" not in out
    out, _ = await _address("Forge, check CI. Friday, we ship the beta", CREW, None, live=True)
    assert out["agents"] == ["frg"]  # the day-use veto still stands


async def test_chat_one_name_and_overlong_lines_ask_no_parts():
    out, decide = await _address(LINE, CREW, _answers(["addressed"] * 3))  # chat
    assert "tasks" not in out and not any(k.startswith("p") for k in decide.await_args.args[1])
    out, _ = await _address("Forge, what our PRs look like", CREW, None, live=True)
    assert out["path"] == "fast" and "tasks" not in out
    long = "Forge, " + " ".join(f"item {i}." for i in range(messaging.MAX_PARTS)) + " Friday, one more."
    out, decide = await _address(long, CREW, _answers(["addressed"] * 2), live=True)
    assert "tasks" not in out and not any(k.startswith("p") for k in decide.await_args.args[1])


async def test_a_slow_live_call_is_asked_twice_and_the_first_answer_wins(monkeypatch):
    import asyncio

    monkeypatch.setattr(messaging, "HEDGE_AFTER_S", 0.01)
    answers = _answers(["addressed"] * 3) | _parts("only_pulse", "only_forge", "only_friday")
    calls = []

    async def decide(state, questions):
        calls.append(len(calls))
        if calls[-1] == 0:
            await asyncio.sleep(30)  # the slow first call; cancelled once the second answers
        return answers

    with patch.object(jev, "decide", decide), patch.object(messaging, "put_metric"):
        out = await messaging.address(_msg(LINE), CREW, history=[], live=True)
    assert calls == [0, 1] and out["path"] == "jev" and out["tasks"]["frg"] == "what our PRs look like"
    calls.clear()

    async def quick(state, questions):
        calls.append(len(calls))
        return answers

    with patch.object(jev, "decide", quick), patch.object(messaging, "put_metric"):
        await messaging.address(_msg(LINE), CREW, history=[], live=True)
        await messaging.address(_msg("Finance and Friday, where are we?"), MEMBERS, history=[])  # chat: never
    assert calls == [0, 1]  # one call each


async def test_live_a_possessive_name_is_words_not_a_cut():
    """Review 2026-10-04: "Forge's PR" cut the line at Forge and asked Jev about "s PR"."""
    text = "Friday, review Forge's PR. Forge, fix the tests"
    answers = _answers(["addressed", "addressed"]) | _parts("only_friday", "only_forge")
    out, decide = await _address(text, CREW, answers, live=True)
    assert _asked_parts(decide) == ["review Forge's PR", "fix the tests"]
    assert out["tasks"] == {"fri": "review Forge's PR", "frg": "fix the tests"}
    curly = "Pulse, update. Friday, review Forge’s PR"
    out, decide = await _address(
        curly, CREW, _answers(["addressed"] * 3) | _parts("only_pulse", "only_friday"), live=True
    )
    assert _asked_parts(decide) == ["update", "review Forge’s PR"]


async def test_live_a_period_inside_a_word_or_number_ends_no_sentence():
    text = "Forge, ship v2.1 to staging. Pulse, churn is 3.5 percent, e.g. last week, write it up"
    answers = _answers(["addressed", "addressed"]) | _parts("only_forge", "only_pulse", "only_pulse")
    out, decide = await _address(text, CREW, answers, live=True)
    assert _asked_parts(decide) == ["ship v2.1 to staging", "churn is 3.5 percent, e.g", "last week, write it up"]
    assert out["tasks"] == {"frg": "ship v2.1 to staging", "pul": "churn is 3.5 percent, e.g. last week, write it up"}


async def test_live_a_name_after_an_agents_last_part_is_not_in_its_task():
    text = "Forge, fix the build. Leo can you call the customer. Pulse, update"
    answers = _answers(["addressed", "mentioned", "addressed"]) | _parts("only_forge", "nobody", "only_pulse")
    out, _ = await _address(text, CREW, answers, live=True)
    assert out["tasks"] == {"frg": "fix the build", "pul": "update"}


async def test_live_a_relaying_agent_keeps_the_name_it_relays_to():
    text = "Pulse, get Gauge to pull the retention numbers"
    answers = _answers(["addressed", "relayed"]) | _parts("only_pulse", "only_pulse")
    out, _ = await _address(text, [*CREW, GAUGE], answers, live=True)
    assert out["tasks"] == {"pul": "get Gauge to pull the retention numbers"}  # Gauge: the whole line


@pytest.mark.parametrize(
    ("text", "part"),
    [
        ("Forge, check CI. Pulse, say hello", "say hello"),
        ("Forge, check CI. Pulse, is the deploy ok", "is the deploy ok"),
        ("Forge, check CI. Pulse, revenue is up 2 million plus", "revenue is up 2 million plus"),
        ("Forge, check CI. Pulse, please advise", "please advise"),
        ("Forge, check CI and then Pulse, update", "check CI"),  # a connector hanging before a name goes
        ("Um Forge, ok so check CI. Pulse, update", "check CI"),  # filler in front goes
    ],
)
async def test_live_only_filler_in_front_and_a_hanging_connector_are_trimmed(text, part):
    _, decide = await _address(text, CREW, _answers(["addressed"] * 2), live=True)
    assert part in _asked_parts(decide)


async def test_a_weekday_keeps_the_models_label_only_where_it_is_called():
    answers = _answers(["addressed", "addressed"])
    out, _ = await _address("The Friday release slipped, we ship by Friday now. Forge?", CREW, answers)
    assert out["agents"] == ["frg"]  # "the Friday release": a day, whatever the model said
    out, _ = await _address("Forge, we ship by Friday. What's the plan, Friday?", CREW, answers)
    assert out["agents"] == ["frg", "fri"]  # called at the end of a clause


async def test_the_stage_s_own_timeout_counts_as_a_jev_timeout_and_cancels_both_calls(monkeypatch):
    import asyncio

    monkeypatch.setattr(messaging, "HEDGE_AFTER_S", 0.01)
    monkeypatch.setattr(messaging, "ADDRESSING_TIMEOUT_S", 0.05)
    cancelled = []

    async def never(state, questions):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    with patch.object(jev, "decide", never), patch.object(messaging, "put_metric") as metric:
        out = await messaging.address(_msg(LINE), CREW, history=[], live=True)
        await asyncio.sleep(0)  # the cancelled calls get their turn
    assert out["path"] == "fallback" and cancelled == [1, 1]
    assert any(c.kwargs.get("dimensions") == {"outcome": "timeout"} for c in metric.call_args_list)


async def test_a_quick_failure_is_not_asked_twice():
    decide = AsyncMock(return_value=None)
    with patch.object(jev, "decide", decide), patch.object(messaging, "put_metric"):
        out = await messaging.address(_msg(LINE), CREW, history=[], live=True)
    assert out["path"] == "fallback" and decide.await_count == 1


async def test_live_a_relayed_part_is_also_the_relayers_and_nobody_elses():
    text = "Pulse, get Gauge to pull the retention numbers. Forge, check the deploy."
    answers = _answers(["addressed", "relayed", "addressed"]) | _parts("only_pulse", "only_gauge", "only_forge")
    out, _ = await _address(text, [*CREW, GAUGE], answers, live=True)
    assert out["tasks"] == {
        "pul": "get Gauge to pull the retention numbers",
        "gau": "to pull the retention numbers",
        "frg": "check the deploy",
    }


async def test_a_name_that_is_only_ever_a_possessive_is_talked_about_not_spoken_to():
    text = "Forge, grab Friday's standup notes. Pulse, update the launch deck."
    answers = _answers(["addressed", "addressed", "addressed"]) | _parts("only_forge", "only_pulse")
    out, _ = await _address(text, CREW, answers, live=True)
    assert out["agents"] == ["frg", "pul"]  # whatever the model said of Friday
    assert out["tasks"] == {"frg": "grab Friday's standup notes", "pul": "update the launch deck"}
    out, _ = await _address("Is Finance's forecast ready? Friday?", MEMBERS, _answers(["addressed", "addressed"]))
    assert out["agents"] == ["fri"]  # chat too
    relayed = _answers(["addressed", "relayed", "addressed"]) | _parts("only_forge", "only_pulse")
    out, _ = await _address(text, CREW, relayed, live=True)
    assert out["agents"] == ["frg", "pul"]  # the bench's poss-02: Jev called Friday relayed
