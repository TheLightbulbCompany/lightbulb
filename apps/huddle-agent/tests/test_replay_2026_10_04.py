"""Replay of the founder's Live call of 2026-10-04 05:44Z (understanding §2, §5.2): a greeting answered twice,
an ask the floor speaker made that nobody ran, three asks run as one blob, and only one of them answered."""

import json

from test_huddle import answer, ops, say, settle
from test_request_engine import quiet, three

L0 = "Hey guys, how's it going?"
L3 = (
    "I got a couple questions. I'm just gonna say them all, and then we'll figure out kind of how to answer them. "
    "I need to know when the update from the application that we made for the money is gonna be in."
)
L5 = (
    "I need you to check Aiden's email to see if we got any response from the application we sent in. I also want "
    "to know what features we've shipped and what PRs are currently open."
)
L8 = "Are you sure that's accurate, or is that old information?"
MONEY = "I need to know when the update from the application that we made for the money is gonna be in"
EMAIL = (
    "check Aiden's email to see if we got any response from the application we sent in"
)
FEATURES = "what features we've shipped"
PRS = "what PRs are currently open"


def notes(lives):
    """Every note a voice got, in order: its seed's developer lines, then thinking and commentary."""
    return [t for x in lives for r, t in x.seed if r == "developer"] + [
        c[1] for x in lives for c in x.calls if c[0] in ("thinking", "commentary")
    ]


def run_for(backend, task):
    return next(t["runId"] for t in backend.turns if t["utterance"] == task)


async def deliver(h, clock, lives, agent):
    """The room goes quiet; `agent`'s own voice opens and says its answer to the end."""
    before = len(lives)
    await quiet(h, clock)
    assert len(lives) == before + 1 and lives[-1].agent_id == agent, (
        f"{agent}'s own voice says its answer"
    )
    assert ("close",) in lives[-2].calls, "one voice at a time"
    h.on_agent_state(agent, "speaking")
    h.on_agent_state(agent, "listening")


async def test_the_2026_10_04_call(capsys):
    h, room, backend, lives, clock, log = await three(people={"u1": "Prasiddha"})

    # L0: small talk, no voice open. Pulse's voice answers it; nothing runs.
    backend.route_extra = ops(social="pulse")
    await say(h, "u1", L0)
    assert backend.turns == [], "no /turns for the greeting"
    assert [x.agent_id for x in lives] == ["pulse"]
    h.on_agent_said("pulse", "Doing well, good to hear you.")
    await quiet(h, clock)

    # L3: the floor speaker announces a list and asks the first thing.
    backend.route_extra = ops(requests=[("pulse", MONEY, "floor")], more=True)
    await say(h, "u1", L3)
    assert [t["utterance"] for t in backend.turns] == [MONEY], (
        "the floor speaker's ask runs"
    )
    await quiet(h, clock)

    # L5: three asks in one line; the voice also delegates the line as one blob.
    backend.route_extra = ops(
        requests=[
            ("pulse", EMAIL, "floor"),
            ("forge", FEATURES, "fit"),
            ("forge", PRS, "fit"),
        ]
    )
    await say(h, "u1", L5)
    assert [t["utterance"] for t in backend.turns] == [MONEY, FEATURES], (
        "Pulse's and Forge's second rows wait"
    )
    await h.on_delegation("pulse", "d5", L5)  # before the quiet second, as on the call
    await settle(h)
    await quiet(h, clock)
    assert len(backend.turns) == 2, "the delegation d5 causes no extra /turns"
    acks = [n for n in notes(lives) if "Forge is on" in n]
    assert len(acks) == 1, "one acknowledgement note naming Forge"
    assert FEATURES in acks[0] and PRS in acks[0], "it names both of Forge's rows"

    # Pulse's first answer, said by its open voice; then its second row starts.
    answer(h, run_for(backend, MONEY), "pulse", "The application update lands Friday.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns] == [MONEY, FEATURES, EMAIL], (
        "Pulse's second row waited"
    )

    # Forge's features answer, said by Forge's own voice; then its PR row starts.
    answer(h, run_for(backend, FEATURES), "forge", "We shipped Live huddles.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns][3:] == [PRS], (
        "Forge's two runs one after the other"
    )
    four = backend.turns[:4]
    assert sorted(t["utterance"] for t in four) == sorted([MONEY, EMAIL, FEATURES, PRS])
    assert all(t["utterance"] in (MONEY, EMAIL, FEATURES, PRS) for t in four), (
        "each /turns has one task"
    )
    await deliver(h, clock, lives, "forge")

    answer(h, run_for(backend, EMAIL), "pulse", "No reply from the application yet.")
    await settle(h)
    await deliver(h, clock, lives, "pulse")

    answer(h, run_for(backend, PRS), "forge", "Two PRs are open.")
    await settle(h)
    await deliver(h, clock, lives, "forge")

    # L8: a follow-up to the PR answer, while Forge's voice holds the floor.
    backend.route_extra = ops(requests=[("forge", L8, "follow")])
    await say(h, "u1", L8)
    assert (
        backend.turns[-1]["utterance"] == L8 and backend.turns[-1]["agentId"] == "forge"
    )
    answer(h, run_for(backend, L8), "forge", "Checked just now: two PRs are open.")
    await settle(h)

    assert len(backend.turns) == 5 and not [
        t for t in backend.turns if L0 in t["utterance"]
    ]
    small = [n for n in notes(lives) if L0 in n]
    assert len(small) == 1, "the greeting is answered once"
    assert [(r.agent, r.status) for r in h._requests.items] == [
        ("pulse", "delivered"),
        ("pulse", "delivered"),
        ("forge", "delivered"),
        ("forge", "delivered"),
        ("forge", "delivered"),
    ], "every row ends delivered"
    await h.clear_presence()
    assert backend.late_answers == [], "every answer was said aloud"
    out = [
        json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")
    ]
    assert [
        m["live.request.open_at_end"] for m in out if "live.request.open_at_end" in m
    ] == [0]
