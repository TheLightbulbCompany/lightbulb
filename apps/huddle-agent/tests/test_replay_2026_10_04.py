"""Replay of the founder's Live call of 2026-10-04 05:44Z (understanding §2, §5.2): a greeting answered twice,
an ask the floor speaker made that nobody ran, three asks run as one blob, and only one of them answered.

Since 10-12 every voice is a mouth (owner ruling): the greeting's reply is written for it once, each ask is
acknowledged by the voice's agent with a fixed line for its own rows (another agent's rows show on its tile), and
each answer is said word for word by its own agent's voice, one at a time."""

import json

from huddle_agent import huddle as h_mod
from test_huddle import answer, ops, say, settle, spoken
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


def run_for(backend, task):
    return next(t["runId"] for t in backend.turns if t["utterance"] == task)


def said(lives):
    """Every line the call's voices were ordered to say, in order."""
    return [c[1] for x in lives for c in x.calls if c[0] == "say"]


async def test_the_2026_10_04_call(capsys):
    h, room, backend, lives, clock, log = await three(people={"u1": "Prasiddha"})
    written = []

    async def writer(persona, event):
        written.append(event)
        return "Doing well, thanks."

    h._writer = writer

    # L0: small talk, no voice open. Pulse's voice says a reply written for it; nothing runs.
    backend.route_extra = ops(social="pulse")
    await say(h, "u1", L0)
    assert backend.turns == [], "no /turns for the greeting"
    assert [x.agent_id for x in lives] == ["pulse"] and said(lives) == ["Doing well, thanks."]
    assert written == [f'Prasiddha just said to you: "{L0}"'], "the greeting is answered once"
    spoken(h, lives[0])
    await quiet(h, clock)

    # L3: the floor speaker announces a list and asks the first thing.
    backend.route_extra = ops(requests=[("pulse", MONEY, "floor")], more=True)
    await say(h, "u1", L3)
    assert [t["utterance"] for t in backend.turns] == [MONEY], "the floor speaker's ask runs"
    assert said(lives)[-1] == h_mod.ACKS[0], "acknowledged with the worker's fixed line"
    spoken(h, lives[0])
    await quiet(h, clock)

    # L5: three asks in one line. Pulse's is next in line for it; Forge's two show on Forge's tile.
    backend.route_extra = ops(
        requests=[
            ("pulse", EMAIL, "floor"),
            ("forge", FEATURES, "fit"),
            ("forge", PRS, "fit"),
        ]
    )
    await say(h, "u1", L5)
    assert [t["utterance"] for t in backend.turns] == [MONEY, FEATURES], "Pulse's and Forge's second rows wait"
    assert said(lives)[-1] == h_mod.QUEUED_LINES[0], "one acknowledgement: Pulse's own row is next for it"
    assert room.written()["lightbulb.agent.forge.status"] == "working"
    spoken(h, lives[0])
    await quiet(h, clock)
    assert len(backend.turns) == 2, "no extra /turns"

    # Pulse's first answer, said by its open voice; then its second row starts.
    answer(h, run_for(backend, MONEY), "pulse", "The application update lands Friday.")
    await settle(h)
    assert said(lives)[-1] == "The application update lands Friday."
    assert [t["utterance"] for t in backend.turns] == [MONEY, FEATURES, EMAIL], "Pulse's second row waited"

    # Forge's features answer, said by Forge's own voice once Pulse has finished; then its PR row starts.
    answer(h, run_for(backend, FEATURES), "forge", "We shipped Live huddles.")
    await settle(h)
    assert [t["utterance"] for t in backend.turns][3:] == [PRS], "Forge's two runs one after the other"
    four = backend.turns[:4]
    assert sorted(t["utterance"] for t in four) == sorted([MONEY, EMAIL, FEATURES, PRS]), "each /turns has one task"
    assert len(lives) == 1, "Pulse is still saying its answer: one agent speaks at a time"
    spoken(h, lives[0])
    await settle(h)
    assert [x.agent_id for x in lives] == ["pulse", "forge"] and ("close",) in lives[0].calls
    assert lives[1].calls == [("say", "We shipped Live huddles.")]
    spoken(h, lives[1])

    answer(h, run_for(backend, EMAIL), "pulse", "No reply from the application yet.")
    await settle(h)
    assert lives[-1].agent_id == "pulse" and lives[-1].calls == [("say", "No reply from the application yet.")]
    spoken(h, lives[-1])

    answer(h, run_for(backend, PRS), "forge", "Two PRs are open.")
    await settle(h)
    assert lives[-1].agent_id == "forge" and lives[-1].calls == [("say", "Two PRs are open.")]
    spoken(h, lives[-1])

    # L8: a follow-up to the PR answer, while Forge's voice holds the floor.
    backend.route_extra = ops(requests=[("forge", L8, "follow")])
    await say(h, "u1", L8)
    assert backend.turns[-1]["utterance"] == L8 and backend.turns[-1]["agentId"] == "forge"
    answer(h, run_for(backend, L8), "forge", "Checked just now: two PRs are open.")
    await settle(h)
    spoken(h, lives[-1])  # its acknowledgement, then the answer it waited behind
    spoken(h, lives[-1])
    assert len(lives) == 4, "no extra voice"
    for _ in range(3):
        await quiet(h, clock)
    assert len(lives) == 4 and len(backend.turns) == 5, "quiet ticks open nothing and run nothing"
    assert said(lives) == [
        "Doing well, thanks.",
        h_mod.ACKS[0],
        h_mod.QUEUED_LINES[0],
        "The application update lands Friday.",
        "We shipped Live huddles.",
        "No reply from the application yet.",
        "Two PRs are open.",
        h_mod.ACKS[1],
        "Checked just now: two PRs are open.",
    ], "each ask acknowledged once, each answer said once, word for word"

    assert not [t for t in backend.turns if L0 in t["utterance"]]
    assert [(r.agent, r.status) for r in h._requests.items] == [
        ("pulse", "delivered"),
        ("pulse", "delivered"),
        ("forge", "delivered"),
        ("forge", "delivered"),
        ("forge", "delivered"),
    ], "every row ends delivered"
    await h.clear_presence()
    assert backend.late_answers == [], "every answer was said aloud"
    out = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    assert [m["live.request.open_at_end"] for m in out if "live.request.open_at_end" in m] == [0]
