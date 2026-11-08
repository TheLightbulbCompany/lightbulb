"""Cost caps: a phone call ends after 20 minutes, a meeting after 90; both env-overridable, both say goodbye first."""

import importlib
import os

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


async def test_a_session_without_a_model_says_a_fixed_goodbye():
    said, done = [], []

    class Session:
        def say(self, text, allow_interruptions):
            said.append(text)

            async def played():
                return None

            return played()

    async def end():
        done.append(said[:])

    await huddle_agent.end_after(0, Session(), end, line="We're out of time.")
    assert done == [["We're out of time."]]
