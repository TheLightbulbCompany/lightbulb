"""Cost caps: a phone call ends after 20 minutes, a meeting after 90; both env-overridable, both say goodbye first."""

import importlib
import os
import time

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

import huddle_agent  # noqa: E402
from huddle_agent import meet, phone  # noqa: E402


def test_defaults_and_env_overrides(monkeypatch):
    assert phone.CALL_MAX_S == 20 * 60 and meet.MEETING_MAX_S == 90 * 60
    monkeypatch.setenv("PHONE_MAX_S", "5")
    monkeypatch.setenv("MEET_MAX_S", "7")
    try:
        assert importlib.reload(phone).CALL_MAX_S == 5
        assert importlib.reload(meet).MEETING_MAX_S == 7
    finally:
        monkeypatch.undo()
        importlib.reload(phone), importlib.reload(meet)


async def test_at_the_limit_it_says_goodbye_then_ends():
    said, done = [], []

    class Speech:
        id = "speech_bye"

        def __await__(self):
            said.append("played")
            yield from ()
            return self

    class Session:
        def generate_reply(self, instructions, allow_interruptions):
            said.append(instructions)
            return Speech()

    async def end():
        done.append(said[:])

    await huddle_agent.end_after(0.01, Session(), end, on_speech=lambda h: said.append(h.id))
    assert done == [[huddle_agent.GOODBYE, "speech_bye", "played"]]  # goodbye's id known before it plays, then end


async def test_a_failed_goodbye_still_ends():
    done = []

    class Session:
        def generate_reply(self, **_):
            raise RuntimeError("session gone")

    async def end():
        done.append(1)

    await huddle_agent.end_after(0, Session(), end)
    assert done == [1]


def test_after_time_up_only_the_goodbye_speech_plays():
    a = meet.MeetAgent(None, meet.Gate(name="Nova"), meet.Mixer(), {"name": "Nova", "company": "c", "title": "t",
                       "today": "d"}, {})  # fmt: skip
    now = time.monotonic()
    a._mixer.voiced_at = now - 10
    a.heard("i1", "Nova, how many calls?", True, now)
    assert a._authorize(now, "speech_held") == (True, "ask")  # before the limit: a normal ask plays
    a.ends_at = now  # time runs out while that reply is still waiting
    a.goodbye_id = "speech_bye"  # end_after's on_speech, from the goodbye's own SpeechHandle
    assert a._authorize(now, "speech_held") == (False, "time up")  # the waiting reply is dropped
    assert a._authorize(now, None) == (False, "time up")
    assert a._authorize(now, "speech_bye") == (True, "goodbye")  # only the goodbye plays
    a.heard("i2", "so anyway the budget is fine", True, now)  # chatter after the goodbye
    assert a._authorize(now, "speech_later") == (False, "time up")


async def test_a_tool_after_time_up_is_refused():
    class B:
        async def meet(self, op, body):
            raise AssertionError("tool reached the backend after time up")

    a = meet.MeetAgent(B(), meet.Gate(name="Nova"), meet.Mixer(), {"name": "Nova", "company": "c", "title": "t",
                       "today": "d"}, {})  # fmt: skip
    a.ends_at = time.monotonic()
    assert await a._post("read", {"kind": "memory"}) == {"error": "time_up"}
    assert await a.call_stats() == {"error": "time_up"}
