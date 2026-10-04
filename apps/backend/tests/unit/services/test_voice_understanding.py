"""Conversation understanding §4.2-§4.4: one spoken line -> the requests on it, their owners, and what is
no request. Jev is stubbed per question key; the real cut, assembly and owner rules run."""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging
from core.services import voice_calls as vc
from tests.unit.services.voice_stubs import jev_says

AGENTS = {"forge": "Forge", "friday": "Friday", "pulse": "Pulse", "gauge": "Gauge"}
PEOPLE = {"user_p": "Prasiddha Parthsarthy", "user_l": "Leo Park"}
MEMBERS = [{"kind": "agent", "id": a, "displayName": n} for a, n in AGENTS.items()] + [
    {"kind": "human", "id": u, "displayName": n, "role": "owner"} for u, n in PEOPLE.items()
]


@pytest.fixture
def decide():
    card = AsyncMock(side_effect=lambda owner, agent, name, recency="": f"{name} card")
    with (
        patch.object(vc, "_room_team", AsyncMock(return_value=({"name": "general", "kind": "channel"}, []))),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(vc.messaging, "human_display_name", AsyncMock(side_effect=lambda u: PEOPLE.get(u, u))),
        patch.object(vc.openclaw_work, "_fallback_ceo_id", AsyncMock(return_value="friday")),
        patch.object(vc.jev, "agent_card", card),
        patch.object(vc.jev, "_decide_for_message", AsyncMock(return_value={"who": "none"})) as judge,
        patch.object(vc.jev, "decide", AsyncMock(return_value=None)) as decide,
    ):
        decide.judge = judge  # the room judge: who answers a line among several people ("none": nobody)
        yield decide


async def route(decide, line, said, *, speaker="user_p", humans=1, floor=None, mine=True, opened=(), **kw):
    decide.return_value = said
    if floor:
        kw["floor"] = {"agent": floor, "speaker": "Prasiddha Parthsarthy", "mine": mine}
    return await vc.route_live_line(
        owner_id="org_a",
        room_id="room-1",
        speaker_id=speaker,
        utterance=line,
        transcript="",
        humans=humans,
        open_requests=list(opened),
        **kw,
    )


def tasks(got):
    return [(r["agentId"], r["task"], r["by"]) for r in got["ops"]["requests"]]


# ---- the cut (§4.2 step 1) ----


def parts(line):
    return [messaging._clean(line[s:e], into) for kind, s, e, _, into in messaging._pieces(line, [], cues=True)]


@pytest.mark.parametrize(
    "line,want",
    [
        (
            "I also want to know what features we've shipped and what PRs are currently open.",
            ["I also want to know what features we've shipped", "what PRs are currently open"],
        ),
        (
            "check if Stripe paid out yesterday, see if any new support tickets came in, and tell me how many opened it",
            [
                "check if Stripe paid out yesterday",
                "see if any new support tickets came in",
                "tell me how many opened it",
            ],
        ),
        (
            "can you see how many trials converted this week and uh is the api still throwing those five hundreds",
            ["can you see how many trials converted this week", "is the api still throwing those five hundreds"],
        ),
        (
            "im gonna rattle off a bunch of stuff and you guys sort it out after um first whats our burn",
            ["im gonna rattle off a bunch of stuff and you guys sort it out after", "first whats our burn"],
        ),
        ("Oh, one more thing, can you check the cert?", ["one more thing", "can you check the cert"]),
        # speech-to-text with no punctuation: a filler, "but", "anyway" or "can you" opens the next part
        (
            "good morning everyone um can you check if the nightly backup ran",
            ["good morning everyone", "can you check if the nightly backup ran"],
        ),
        (
            "the tweet's good but can you make a LinkedIn version too",
            ["the tweet's good", "can you make a LinkedIn version too"],
        ),
        (
            "sorry my dog is barking lol anyway how many tickets came in overnight",
            ["sorry my dog is barking lol", "anyway how many tickets came in overnight"],
        ),
        (
            "whats our burn uh how many active users do we have and is the api down",
            ["whats our burn", "how many active users do we have", "is the api down"],
        ),
        ("nice and what about week over week retention", ["nice", "what about week over week retention"]),
        (
            "I'll just rattle them off first one is can you find the Acme NDA",
            ["I'll just rattle them off", "first one is", "can you find the Acme NDA"],
        ),
        ("Anyway, how many trials started yesterday?", ["Anyway", "how many trials started yesterday"]),
    ],
)
def test_a_sentence_is_cut_where_a_new_request_begins(line, want):
    assert parts(line) == want


@pytest.mark.parametrize(
    "line",
    [
        "Compare signups, activation and paid conversion for August and September.",
        "check the Stripe dashboard and the Mercury balance",
        "Can you find the terms and conditions page that legal sent over?",
        "What did we pay for Figma, Notion, Linear and Slack last year?",
        "Pull the signups for, uh, July, sorry, I mean August.",
        "I'm just gonna say them all, and then we'll figure out kind of how to answer them.",
        "okay can you also rerun the flaky e2e job on the mobile branch",  # "can you" alone is no part
        "export the list of trial users and then send it to Dana",  # "send it" leans on the export: one task
        "check the build and test results",
        "find the customer who emailed us and asked for a refund",
        "what's the open rate on the um the last two newsletters actually just the last one",
    ],
)
def test_one_ask_with_several_objects_is_not_cut(line):
    assert len(parts(line)) == 1


# ---- requests and owners (§4.3) ----

LINE_5 = (
    "I need you to check Aiden's email to see if we got any response from the application we sent in. "
    "I also want to know what features we've shipped and what PRs are currently open."
)


async def test_three_asks_in_one_breath_are_three_requests_with_their_owners(decide):
    """The founder's call, 2026-10-04: prod sent one blob to the floor agent and answered one of three."""
    said = jev_says(kinds=["request"] * 3, whos=[None, "forge", "forge"])
    got = await route(decide, LINE_5, said, floor="pulse")
    assert tasks(got) == [
        (
            "pulse",
            "I need you to check Aiden's email to see if we got any response from the application we sent in",
            "floor",
        ),
        ("forge", "I also want to know what features we've shipped", "fit"),
        ("forge", "what PRs are currently open", "fit"),
    ]
    assert got["agentIds"] == ["pulse", "forge"] and got["ops"]["social"] is None
    # what a worker before ``ops`` splits on: one request per agent
    assert [r["agentId"] for r in got["requests"]] == ["pulse", "forge"]
    assert got["requests"][1]["task"].endswith("what PRs are currently open")
    questions = decide.await_args.args[1]
    assert {"k0", "k1", "k2", "w0", "w1", "w2"} <= set(questions) and "amends" not in questions
    assert set(questions["w0"]["criteria"]) == {*AGENTS, "unsure"}
    assert decide.await_args.args[0]["room"]["floor"] == "Pulse is talking with Prasiddha Parthsarthy"


async def test_a_greeting_is_no_request(decide):
    """Prod started a backend run for "Hey guys, how's it going?" and greeted twice."""
    hello = jev_says(kinds=["social", "social"])  # "Hey guys," | "how's it going"
    got = await route(decide, "Hey guys, how's it going?", hello)
    assert got["ops"] == {"requests": [], "social": "friday", "more": False, "cancel": [], "steer": []}
    assert got["agentIds"] == ["friday"] and "requests" not in got
    two = await route(decide, "Hey guys, how's it going?", hello, humans=2)
    assert two["ops"]["social"] is None and two["agentIds"] == []  # people greeting each other
    open_voice = await route(decide, "Thanks, that's helpful.", jev_says(kinds=["social"]), humans=2, floor="gauge")
    assert open_voice["ops"]["social"] == "gauge"  # the floor conversation


async def test_an_announcement_then_a_request(decide):
    line = "I got a couple questions. I'm just gonna say them all. I need to know when the update is in."
    got = await route(decide, line, jev_says(kinds=["more", "more", "request"], whos=[None, None, None]))
    assert tasks(got) == [("friday", "I need to know when the update is in", "chief")]
    assert got["ops"]["more"] is True
    alone = await route(decide, "I have a few questions, bear with me.", jev_says(kinds=["more"]))
    assert alone["ops"] == {"requests": [], "social": "friday", "more": True, "cancel": [], "steer": []}


async def test_a_named_agent_owns_every_ask_on_its_line(decide):
    line = "Forge, check CI, then tell me what shipped."
    said = jev_says(kinds=["request", "request"], whos=["pulse", "pulse"], names=["addressed"])
    assert tasks(await route(decide, line, said)) == [
        ("forge", "check CI", "name"),
        ("forge", "tell me what shipped", "name"),
    ]


async def test_a_part_handed_to_someone_goes_by_fit_not_to_the_named_agent(decide):
    line = "Gauge, how many people used export? And somebody find out why the widget stopped loading."
    said = jev_says(kinds=["request", "request"], whos=["gauge", "forge"], names=["addressed"])
    assert tasks(await route(decide, line, said)) == [
        ("gauge", "how many people used export", "name"),
        ("forge", "somebody find out why the widget stopped loading", "fit"),
    ]


async def test_two_named_agents_keep_their_own_parts(decide):
    line = "Forge, check the deploy, and Friday, summarize yesterday's standup."
    said = jev_says(
        kinds=["request", "request"],
        whos=[None, None],
        names=["addressed", "addressed"],
        asked=["only_forge", "only_friday"],
    )
    assert tasks(await route(decide, line, said)) == [
        ("forge", "check the deploy", "name"),
        ("friday", "summarize yesterday's standup", "name"),
    ]


async def test_background_belongs_to_the_request_beside_it(decide):
    line = "We shipped the new pricing page on Tuesday. Has conversion moved since then?"
    got = await route(decide, line, jev_says(kinds=["context", "request"], whos=[None, "gauge"]))
    assert tasks(got) == [
        ("gauge", "We shipped the new pricing page on Tuesday. Has conversion moved since then", "fit")
    ]
    remark = await route(decide, "Man, the onboarding emails feel off lately.", jev_says(kinds=["context"]))
    assert tasks(remark) == [] and remark["ops"]["social"] == "friday"  # nothing asked: no run
    to_forge = "Forge, the deploy looks broken."
    told = await route(decide, to_forge, jev_says(kinds=["context"], names=["addressed"]))
    assert tasks(told) == [("forge", "the deploy looks broken", "name")]  # said to an agent: it looks


async def test_a_weak_fit_goes_to_the_floor_agent_else_the_chief(decide):
    said = jev_says(kinds=["request"])
    said["w0"] = {"choice": "gauge", "probabilities": {"gauge": 0.3, "pulse": 0.28, "forge": 0.22}}
    assert tasks(await route(decide, "what's our MRR", said, floor="pulse")) == [("pulse", "what's our MRR", "floor")]
    assert tasks(await route(decide, "what's our MRR", said)) == [("friday", "what's our MRR", "chief")]
    not_mine = await route(decide, "what's our MRR", said, floor="pulse", mine=False)
    assert tasks(not_mine) == [("friday", "what's our MRR", "chief")]


# ---- earlier requests (§4.4: steer, follow-up, cancel) ----

OPEN = [
    {"id": "r1", "agentId": "gauge", "task": "how many signups came in last week", "status": "working"},
    {"id": "r2", "agentId": "forge", "task": "what PRs are open", "status": "delivered"},
]


async def test_a_correction_steers_the_working_request(decide):
    line = "Actually make that last month, not last week."
    got = await route(decide, line, jev_says(kinds=["request"], amends="r1"), opened=OPEN)
    assert got["ops"]["steer"] == [{"requestId": "r1", "text": "Actually make that last month, not last week"}]
    assert tasks(got) == [] and got["agentIds"] == ["gauge"]
    questions = decide.await_args.args[1]
    assert set(questions["amends"]["criteria"]) == {"r1", "r2", "none"}
    assert 'Gauge: "how many signups came in last week" (working)' in decide.await_args.args[0]["earlierRequests"]
    detail = await route(decide, "only the ones on main", jev_says(kinds=["context"], amends="r1"), opened=OPEN)
    assert detail["ops"]["steer"] == [{"requestId": "r1", "text": "only the ones on main"}]


async def test_a_follow_up_to_an_answer_goes_to_who_answered(decide):
    line = "Are you sure that's accurate, or is that old information?"
    got = await route(decide, line, jev_says(kinds=["request"], whos=["gauge"], amends="r2"), opened=OPEN)
    assert tasks(got) == [("forge", "Are you sure that's accurate, or is that old information", "follow")]
    assert got["ops"]["steer"] == []


async def test_a_request_named_to_another_agent_is_new_not_a_steer(decide):
    said = jev_says(kinds=["request"], names=["addressed"], amends="r1")
    got = await route(decide, "Forge, do the same for the paid plans.", said, opened=OPEN)
    assert tasks(got) == [("forge", "do the same for the paid plans", "name")] and got["ops"]["steer"] == []


async def test_the_wake_word_is_said_to_the_agents_whatever_jev_hears(decide):
    got = await route(decide, "did you call the landlord", jev_says(kinds=["people"]), humans=0, wake=True)
    assert tasks(got) == [("friday", "did you call the landlord", "chief")]
    meet = await route(decide, "morning", jev_says(kinds=["social"]), humans=3, setting="meeting")
    assert decide.await_args.args[0]["room"]["setting"] == vc.jev.MEETING_CONTEXT and meet["agentIds"] == []


async def test_calling_a_request_off_cancels_it(decide):
    # "Forget" is heard as a possible "Forge" (the worker's fuzzy match): Jev says it is not the name
    said = jev_says(kinds=["cancel"], names=["not_a_name"], drops="r1")
    got = await route(decide, "Forget the signups thing.", said, opened=OPEN)
    assert got["ops"]["cancel"] == ["r1"] and tasks(got) == [] and got["ops"]["social"] is None
    said = jev_says(kinds=["cancel"], names=["not_a_name"], drops="r2")
    done = await route(decide, "Forget the PR thing.", said, opened=OPEN)
    assert done["ops"]["cancel"] == []  # already answered: nothing to call off


# ---- who is implied (§4.4) ----


async def test_among_several_people_the_room_judge_says_whether_an_agent_answers(decide):
    line = "Can we get the churn numbers for September?"
    said = jev_says(kinds=["request"], whos=["gauge"])
    quiet = await route(decide, line, said, humans=2)
    assert tasks(quiet) == [] and quiet["agentIds"] == []  # the judge: people talking, nobody answers
    assert decide.judge.await_args.kwargs["to_people"] is False
    decide.judge.return_value = {"who": "forge"}
    one = await route(decide, line, said, humans=2)
    # one ask: the judge read the conversation, so its teammate owns it, whatever fits the part
    assert tasks(one) == [("forge", "Can we get the churn numbers for September", "room")]
    two = "can one of you pull churn for September and also check why the deploy failed"
    both = await route(decide, two, jev_says(kinds=["request"] * 2, whos=["gauge", None]), humans=2)
    assert tasks(both) == [
        ("gauge", "can one of you pull churn for September", "fit"),
        ("forge", "check why the deploy failed", "room"),
    ]
    chime = await route(decide, "I have no idea what our runway is", jev_says(kinds=["people"]), humans=2)
    assert tasks(chime) == [("forge", "I have no idea what our runway is", "room")]  # the judge heard a gap to fill
    decide.judge.reset_mock()
    mine = await route(decide, line, jev_says(kinds=["request"]), humans=2, floor="pulse")
    assert tasks(mine) == [("pulse", "Can we get the churn numbers for September", "floor")]
    decide.judge.assert_not_awaited()  # the floor conversation needs no judge


async def test_the_room_judge_down_falls_back_like_chat(decide):
    decide.judge.return_value = {"fallback": True}
    got = await route(decide, "what's our burn", jev_says(kinds=["request"]), humans=2)
    assert tasks(got) == [("friday", "what's our burn", "room")]
    decide.judge.return_value = {"who": "none", "forced": "question"}
    assert tasks(await route(decide, "what's our burn", jev_says(kinds=["request"]), humans=2))[0][0] == "friday"


async def test_said_to_a_person_is_for_nobody(decide):
    said = jev_says(kinds=["request"], whos=["forge"], names=["addressed"])
    got = await route(decide, "Leo, can you send me that deck after this?", said, humans=2, floor="gauge")
    assert got["ops"] == {"requests": [], "social": None, "more": False, "cancel": [], "steer": []}
    typed = await route(decide, "did you call the landlord", jev_says(kinds=["people"]), floor="gauge")
    assert typed["ops"]["requests"] == [] and typed["ops"]["social"] is None


async def test_a_meeting_implies_nobody_but_the_named_the_wake_word_and_the_floor(decide):
    line = "Can we get the conversion numbers for last quarter?"
    said = jev_says(kinds=["request"], whos=["gauge"])
    assert tasks(await route(decide, line, said, humans=3, setting="meeting")) == []
    assert tasks(await route(decide, line, said, humans=0, wake=True, setting="meeting")) == [
        ("gauge", "Can we get the conversion numbers for last quarter", "fit")
    ]
    assert tasks(await route(decide, line, said, humans=3, setting="meeting", floor="forge"))[0][0] == "gauge"
    hello = await route(decide, "morning all", jev_says(kinds=["social"]), humans=1, setting="meeting")
    assert hello["ops"]["social"] is None  # nobody greets a meeting unasked
    asked = await route(
        decide, "what's our burn?", jev_says(kinds=["people"]), humans=3, setting="meeting", unclaimed=True
    )
    assert tasks(asked) == [("friday", "what's our burn?", "chief")]  # nobody took it: one agent answers


# ---- failure and limits ----


async def test_jev_down_is_one_request_for_the_named_or_the_implied(decide):
    named = await route(decide, "Forge, check CI, then tell me what shipped.", None)
    assert tasks(named) == [("forge", "Forge, check CI, then tell me what shipped.", "name")]
    alone = await route(decide, "what's our burn and what shipped", None)
    assert tasks(alone) == [("friday", "what's our burn and what shipped", "chief")]
    floor = await route(decide, "what's our burn", None, humans=2, floor="pulse")
    assert tasks(floor) == [("pulse", "what's our burn", "floor")]
    assert tasks(await route(decide, "what's our burn", None, humans=2)) == []  # two people: silence


async def test_an_unfinished_line_is_held(decide):
    got = await route(decide, "Forge, we're trying to", jev_says(kinds=["request"], names=["addressed"], complete=0.2))
    assert got["hold"] is True and got["agentId"] == "forge"


async def test_a_line_with_too_many_parts_is_one_request(decide):
    line = ", and ".join(f"check the number {i} deploy" for i in range(10))
    got = await route(decide, line, jev_says(kinds=["request"], whos=["forge"]))
    assert tasks(got) == [("forge", line, "fit")]
    assert "k1" not in decide.await_args.args[1]
    # two agents named on such a line: each gets the whole line (nothing says whose part is whose)
    named = f"Forge and Gauge, {line}"
    both = await route(decide, named, jev_says(kinds=["request"], names=["addressed", "addressed"]))
    assert [(a, by) for a, _, by in tasks(both)] == [("forge", "name"), ("gauge", "name")]


async def test_who_is_asked_is_not_a_request_of_its_own(decide):
    line = "Pulse, schedule the promo, and whoever handles billing can you check why Hooli got charged twice"
    said = jev_says(kinds=["request", "request"], whos=["pulse", "forge"], names=["addressed"])
    assert tasks(await route(decide, line, said)) == [
        ("pulse", "schedule the promo", "name"),
        ("forge", "whoever handles billing can you check why Hooli got charged twice", "fit"),
    ]
