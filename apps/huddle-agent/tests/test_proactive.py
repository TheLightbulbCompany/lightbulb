import pytest

from huddle_agent import gate
from huddle_agent import huddle as h_mod
from huddle_agent.backend import BackendError
from huddle_agent.huddle import ANNOUNCE_NOW, NEWS_TTL, NEWS_PER_AGENT
from test_huddle import answer, make, say, settle


@pytest.fixture
def emitted(monkeypatch):
    out = []
    monkeypatch.setattr(h_mod, "emit_metric", lambda name, value, unit="Seconds", dims=None: out.append(name))
    return out


async def with_news(*agents):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha", "u2": "Aden"})
    for i, agent_id in enumerate(agents):
        h._news.setdefault(agent_id, []).append((f"r-{agent_id}", f"the {agent_id} task", f"{agent_id} is done", clock.t + i, "u2"))
    return h, room, backend, lives, clock, log


async def quiet_tick(h, clock, seconds=gate.ANNOUNCE_QUIET):
    clock.t += seconds
    await h.tick()
    await settle(h)


async def test_announced_by_its_own_agent_after_quiet(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    clock.t += 1
    await h.tick()
    assert lives == []  # not quiet long enough
    await quiet_tick(h, clock)
    assert backend.sessions == ["forge"] and lives[0].speaker == "u2"  # pinned to who asked
    assert lives[0].seed[-1] == ("developer", "You finished the forge task: forge is done. Tell the room now.")
    assert lives[0].calls == [("instructions", ANNOUNCE_NOW), ("nudge",)]
    assert "forge" in h._news  # not consumed until its audio starts
    h.on_agent_state("forge", "speaking")
    assert "forge" not in h._news and emitted.count("live.announce.spoken") == 1


async def test_no_announce_while_someone_speaks_or_a_voice_is_open(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    h.on_active_speakers({"u1"})
    await quiet_tick(h, clock, 10)
    assert lives == []
    h.on_active_speakers(set())
    await quiet_tick(h, clock, 1)
    assert lives == []  # the room just went quiet
    await say(h, "u1", "Gauge, hi")
    await quiet_tick(h, clock, 5)
    assert [l.agent_id for l in lives] == ["gauge"]  # the open voice holds; no announcement


async def test_oldest_first_across_agents(emitted):
    h, room, backend, lives, clock, log = await with_news("gauge", "forge")
    await quiet_tick(h, clock)
    assert backend.sessions == ["gauge"]


async def test_speech_before_audio_keeps_the_news(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    await quiet_tick(h, clock)
    h.on_active_speakers({"u1"})
    await settle(h)
    assert ("interrupt",) in lives[0].calls and h._voice is None and "close" in log
    assert h._news["forge"] and "live.announce.aborted" in emitted


async def test_speech_during_audio_cuts_and_consumes(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    await quiet_tick(h, clock)
    h.on_agent_state("forge", "speaking")
    h.on_active_speakers({"u1"})
    assert lives[0].calls[-1] == ("interrupt",) and "live.announce.cut" in emitted
    assert "forge" not in h._news and h._voice is not None  # it listens on, like any voice


async def test_rate_limited_between_announcements(emitted):
    h, room, backend, lives, clock, log = await with_news("forge", "gauge")
    await quiet_tick(h, clock)
    h.on_agent_state("forge", "speaking")
    await h.end_exchange()
    await quiet_tick(h, clock, 10)
    assert backend.sessions == ["forge"]  # gauge waits for the gap
    await quiet_tick(h, clock, gate.MIN_GAP)
    assert backend.sessions == ["forge", "gauge"]


async def test_a_tap_announces_past_the_rate_limit(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    h._rate.record(clock.t)
    await h.talk_start("u1", '{"agentId": "forge"}')
    assert lives[0].speaker == "u1" and lives[0].calls == [("instructions", ANNOUNCE_NOW), ("nudge",)]
    assert lives[0].seed[-1][1].startswith("You finished the forge task")


async def test_a_billing_refusal_keeps_the_badge_silently(emitted):
    h, room, backend, lives, clock, log = await with_news("forge")
    backend.summon_error = BackendError(402, "voice_limit", "credits_low")
    await quiet_tick(h, clock)
    assert lives == [] and h._news["forge"] and "live.announce.refused" in emitted
    assert not [p for t, p, to in room.published if t == "huddle.error"]
    assert room.written()["lightbulb.agent.forge.status"] == "has_news"
    n = len(backend.summons)
    await quiet_tick(h, clock)
    assert len(backend.summons) == n  # the attempt counted against the rate limit


async def test_news_ttl_and_per_agent_cap(emitted):
    h, room, backend, lives, clock, log = make()
    await h.on_participants({"u1": "Prasiddha"})
    for i in range(NEWS_PER_AGENT + 1):
        answer(h, f"r{i}", "gauge", f"Answer {i}.")
    assert len(h._news["gauge"]) == NEWS_PER_AGENT
    h.on_active_speakers({"u1"})  # someone talks the whole time: nothing announces
    clock.t += NEWS_TTL
    await h.tick()
    assert "gauge" not in h._news and lives == []
