"""An answer whose agent's voice is closed: its own agent's voice opens for it at a pause (`Huddle._open_for`).
The GPT-Live proactive announcements (news, a rate limit, a seed to say it in its own words) are gone with
GPT-Live (owner ruling 10-12): an answer nobody here asked for goes to the room in writing at once."""

import pytest

from huddle_agent import gate
from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from test_huddle import hand, make, settle


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(h_mod, "emit_metric", lambda name, value, unit="Seconds", dims=None: out.append(name))
    return out


async def quiet_tick(h, clock, seconds=gate.ANNOUNCE_QUIET):
    clock.t += seconds
    await h.tick()
    await settle(h)


async def owed(agent="forge", audience="u2"):
    """An answer `audience` asked `agent` for, ready, with no voice of its agent open."""
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    h._utter(agent, "answer", f"{agent.title()} is done.", obligation=f"r-{agent}", request=f"the {agent} task", audience=audience)
    return h, room, backend, lives, clock, log


async def test_said_by_its_own_agent_at_a_pause_to_who_asked(emitted):
    h, room, backend, lives, clock, log = await owed()
    clock.t += 1
    await h.tick()
    assert lives == []  # not a pause yet
    await quiet_tick(h, clock)
    assert backend.sessions == ["forge"] and lives[0].speaker == "u2" and h._voice.path == "announce"
    assert lives[0].calls == [("say", "Forge is done.")]  # word for word: no model says it in its own words


async def test_a_billing_refusal_keeps_the_hand_up_silently_then_posts_it(emitted):
    h, room, backend, lives, clock, log = await owed()
    backend.summon_error = BackendError(402, "voice_limit", "credits_low")
    await quiet_tick(h, clock)
    assert lives == [] and hand(h, "forge") == ["Forge is done."] and "live.announce.refused" in emitted
    assert not [p for t, p, to in room.published if t == "huddle.error"]
    assert room.written()["lightbulb.agent.forge.status"] == "has_news"
    n = len(backend.summons)
    await quiet_tick(h, clock, 1)
    assert len(backend.summons) == n  # tried again after gate.ANNOUNCE_QUIET, not every tick
    for _ in range(h_mod.DELIVERY_TRIES):
        await quiet_tick(h, clock)
    assert backend.late_answers == [("forge", "r-forge", "the forge task", "Forge is done.")]
    assert hand(h, "forge") == [] and room.written()["lightbulb.agent.forge.status"] == "idle"
