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
