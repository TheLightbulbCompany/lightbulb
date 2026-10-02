"""Proactive speech in the huddle: news and follow-ups behind the gate (huddle.py + gate.py)."""

import json

import pytest

from huddle_agent import gate
from test_huddle import make, packet, settle


def gated(backend, p):
    backend.gates = []

    async def gate_call(body):
        backend.gates.append(body)
        return {"pSpeak": p, "kind": None}

    backend.gate = gate_call


async def two_people(p=0.9):
    h, room, backend, lives, clock, log = make()
    gated(backend, p)
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    return h, room, backend, lives, clock, log


async def orphaned_result(h, backend, clock, text="Q3 revenue is up 12%."):
    """Lightbulb's exchange ends while Finance is still working; Finance finishes afterwards."""
    await h.on_final_transcript("u1", "Prasiddha", "hey Lightbulb get Finance on the Q3 numbers")
    await settle(h)
    await h.on_delegation("d1", "Q3 numbers")
    await h.end_exchange()
    clock.t += 30
    h.on_data(None, "call.frame", packet({"type": "chunk", "content": text, "seq": 1}))
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 2}))
    await settle(h)


def metrics(capsys):
    return [next(k for k in json.loads(line) if k != "_aws") for line in capsys.readouterr().out.splitlines()]


async def test_news_after_the_exchange_ended_is_announced(capsys):
    h, room, backend, lives, clock, log = await two_people(0.9)
    await orphaned_result(h, backend, clock)
    assert len(backend.gates) == 1 and backend.gates[0]["pendingNews"] == ["Finance: Q3 revenue is up 12%."]
    assert backend.summons == ["u1", "u1"] and len(lives) == 2
    news = lives[1].calls
    assert news[-2][0] == "thinking" and "- Finance: Q3 revenue is up 12%." in news[-2][1]
    assert news[-1] == ("nudge",)
    h.on_agent_state("speaking")
    assert h._news == {}  # the room heard it
    assert {"live.gate.calls", "live.gate.speak"} <= set(metrics(capsys))


async def test_news_is_deduped_by_turn_and_expires():
    h, room, backend, lives, clock, log = await two_people(0.0)
    await orphaned_result(h, backend, clock)
    h.on_data(None, "call.frame", packet({"type": "run_ended", "seq": 3}))  # a replayed run_ended
    assert len(h._news) == 1
    clock.t += gate.NEWS_TTL
    assert h._news_items() == []


async def test_hand_raised_between_thresholds_and_tap_hears_the_news(capsys):
    h, room, backend, lives, clock, log = await two_people(0.7)
    await orphaned_result(h, backend, clock)
    assert len(lives) == 1  # no audio
    assert room.attrs[-1]["lightbulb.hand"] == "1"
    assert "live.gate.hand" in metrics(capsys)
    await h.talk_start("u2")
    assert backend.summons[-1] == "u2" and lives[1].calls[-1] == ("nudge",)
    assert "Q3 revenue" in lives[1].calls[-2][1]
    assert room.attrs[-1]["lightbulb.hand"] == ""


async def test_hand_lowers_after_15s():
    h, room, backend, lives, clock, log = await two_people(0.7)
    await orphaned_result(h, backend, clock)
    clock.t += gate.HAND_SECONDS
    await h.tick()
    assert room.attrs[-1]["lightbulb.hand"] == ""


async def test_below_hand_threshold_does_nothing():
    h, room, backend, lives, clock, log = await two_people(0.3)
    await orphaned_result(h, backend, clock)
    assert len(lives) == 1 and room.attrs[-1]["lightbulb.hand"] == ""


async def follow_up_setup(p=0.9):
    h, room, backend, lives, clock, log = await two_people(p)
    await h.on_final_transcript("u1", "Prasiddha", "Lightbulb, how did Q2 go?")
    await settle(h)
    h.on_agent_state("speaking")
    h.on_agent_state("listening")
    h.on_agent_said("Q2 revenue was up 8% on Q1.")
    return h, room, backend, lives, clock, log


async def test_lightbulbs_lines_join_the_transcript():
    h, room, backend, lives, clock, log = await follow_up_setup()
    assert h._transcript.text().endswith("Lightbulb: Q2 revenue was up 8% on Q1.")


async def test_follow_up_from_someone_else_inside_the_window_speaks():
    h, room, backend, lives, clock, log = await follow_up_setup(0.9)
    clock.t += 5
    await h.on_final_transcript("u2", "Aden", "and what about Q3?")
    await settle(h)
    assert backend.gates[0]["lightbulbLastSaid"] == "Q2 revenue was up 8% on Q1."
    assert backend.gates[0]["transcript"][-1] == {"author": "Aden", "text": "and what about Q3?"}
    assert lives[0].calls[-3:] == [
        ("set_participant", "u2"),
        ("thinking", "Aden is talking to you", None),
        ("reply_to", "and what about Q3?"),
    ]
    assert backend.summons == ["u1"]  # the open call, re-pointed


async def test_follow_up_outside_the_window_does_not_gate():
    h, room, backend, lives, clock, log = await follow_up_setup(0.99)
    await h.end_exchange()
    clock.t += gate.FOLLOWUP_SECONDS + 1
    await h.on_final_transcript("u2", "Aden", "and what about Q3?")
    await settle(h)
    assert backend.gates == [] and backend.summons == ["u1"]


async def test_follow_up_after_the_exchange_closed_opens_one():
    h, room, backend, lives, clock, log = await follow_up_setup(0.9)
    await h.end_exchange()
    clock.t += 3
    await h.on_final_transcript("u1", "Prasiddha", "and Q3?")
    await settle(h)
    assert backend.summons == ["u1", "u1"] and lives[1].calls[-1] == ("reply_to", "and Q3?")


async def test_backchannel_and_addressed_lines_never_call_the_gate():
    h, room, backend, lives, clock, log = await follow_up_setup(0.99)
    for line in ("yeah thanks", "Prasiddha, does that match your sheet?", "so the thing is,"):
        clock.t += 1
        await h.on_final_transcript("u2", "Aden", line)
        await settle(h)
    assert backend.gates == []


async def test_room_not_silent_skips_the_decision():
    h, room, backend, lives, clock, log = await follow_up_setup(0.99)
    clock.t += 2
    h.on_active_speakers({"u1"})  # Prasiddha is talking
    await h.on_final_transcript("u2", "Aden", "and what about Q3?")
    await settle(h)
    assert backend.gates == []


async def test_room_recheck_aborts_the_speak(capsys):
    h, room, backend, lives, clock, log = await follow_up_setup(0.9)
    await h.end_exchange()
    n = len(lives)

    async def gate_then_someone_talks(body):
        h.on_active_speakers({"u1"})
        return {"pSpeak": 0.95, "kind": "I2a"}

    backend.gate = gate_then_someone_talks
    clock.t += 2
    await h.on_final_transcript("u2", "Aden", "and what about Q3?")
    await settle(h)
    assert len(lives) == n and backend.summons == ["u1"]
    assert "live.proactive.aborted" in metrics(capsys)


async def test_voice_before_audio_aborts_and_ends_a_proactive_exchange(capsys):
    h, room, backend, lives, clock, log = await two_people(0.9)
    await orphaned_result(h, backend, clock)
    assert h._x is not None and h._x.proactive
    h.on_active_speakers({"u2"})
    await settle(h)
    assert ("interrupt",) in lives[1].calls and h._x is None
    assert h._news  # not heard: still queued
    assert "live.proactive.aborted" in metrics(capsys)


async def test_voice_during_proactive_audio_cuts_it(capsys):
    h, room, backend, lives, clock, log = await two_people(0.9)
    await orphaned_result(h, backend, clock)
    h.on_agent_state("speaking")
    h.on_active_speakers({"u2"})
    assert lives[1].calls[-1] == ("interrupt",)
    assert "live.proactive.cut" in metrics(capsys)


async def test_rate_limit_holds():
    h, room, backend, lives, clock, log = await two_people(0.9)
    await orphaned_result(h, backend, clock)
    await h.end_exchange()
    h._queue_news("t9", "Growth: the campaign is live.")
    await settle(h)
    assert len(lives) == 2  # the second speak inside 90 s is held
    clock.t += gate.MIN_GAP
    h._queue_news("t10", "Growth: signups doubled.")
    await settle(h)
    assert len(lives) == 3


async def test_kill_switch(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_PROACTIVE", "off")
    h, room, backend, lives, clock, log = await two_people(0.99)
    await orphaned_result(h, backend, clock)
    assert backend.gates == [] and len(lives) == 1


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("LIGHTBULB_PROACTIVE", raising=False)
