"""8 Nov prod booking smoke after #5123 (evidence/e2e-prod-2026-11-08/phone.md).

1. The caller's English first line reached the room as "はい。" / "Szerelt?": the realtime model's input transcription
   (livekit default gpt-4o-mini-transcribe, no language) guessed the language, and Nova replied in Japanese.
2. One team message was spoken 3 times, rephrased, no cut logged (move-125915): say(seq 3) put the team's message in
   as the newest user turn (#5123), and the two fixed lines queued behind it ("Let me check.", the 8 s holding line)
   carried only instructions, so the model answered that newest turn again. Every fixed line now brings its own turn.
"""

import pytest

from huddle_agent import meet, mouth, owner, phone
from huddle_agent.mouth import Mouth

INFO = {"assistantName": "Nova", "businessName": "The Lightbulb Company", "greeting": "Hi", "today": "Sunday"}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")


def test_phone_and_orb_transcribe_english_and_meet_stt_does():
    for model in (phone.realtime_model(), owner.realtime_model()):
        tr = model._opts.input_audio_transcription
        assert tr is not None and tr.language == "en"
    assert meet.realtime_model()._opts.input_audio_transcription is None  # Meet hears through its STT
    assert meet.STT_LANGUAGE == "en"


def test_every_voice_replies_in_english():
    texts = (
        phone.INSTRUCTIONS,
        owner.INSTRUCTIONS,
        meet.INSTRUCTIONS,
    )
    for t in texts:
        assert mouth.ENGLISH in t


class _Speech:
    def __init__(self, sid):
        self.id, self.callbacks = sid, []

    def add_done_callback(self, cb):
        self.callbacks.append(cb)


class _Session:
    current_speech = None

    def __init__(self):
        self.kw: list[dict] = []

    def generate_reply(self, **kw):
        self.kw.append(kw)
        return _Speech(f"sp_{len(self.kw)}")


class _Backend:
    call_id = "c1"

    async def post(self, path, body=None):
        return {}


async def test_a_team_message_is_never_the_newest_turn_for_a_fixed_line(monkeypatch):
    """move-125915: the result, then the holding line and the 8 s line queued behind it, each said the result."""
    s = _Session()
    monkeypatch.setattr(phone.Receptionist, "session", property(lambda _self: s))
    r = phone.Receptionist(_Backend(), INFO, None)
    r.say(["Taylor, I couldn't move your booking."])
    r.delegate("Okay, then how about Monday afternoon instead?")
    phone.line(s, phone.HOLD_AGAIN)
    await r.turns.flush()
    team, hold, again = s.kw
    assert team["user_input"].startswith(mouth.TEAM)
    for kw in (hold, again):  # each fixed line is its own newest turn, so the model says it, not the result
        assert mouth.team_line(kw["user_input"]) and kw["instructions"] in kw["user_input"]
        assert "couldn't move" not in kw["user_input"]


async def test_each_message_id_is_spoken_exactly_once():
    batches = [
        {"messages": [{"id": "m3", "seq": 3, "text": "Closed Sundays."}], "after": 3},
        {"messages": [{"id": "m3", "seq": 3, "text": "Closed Sundays."},
                      {"id": "m5", "seq": 5, "text": "Monday 1, 3 or 5 PM?"}], "after": 5},
        {"messages": [{"id": "m5", "seq": 5, "text": "Monday 1, 3 or 5 PM?"}], "after": 5},
    ]

    async def fetch(after, handled):
        return batches.pop(0) if batches else {"messages": [], "after": 5}

    m, said = Mouth(fetch, 0), []
    for _ in range(4):
        await m.poll()
        m.speak(said.append, ready=True, live=True)
    assert said == [["Closed Sundays."], ["Monday 1, 3 or 5 PM?"]]


def test_a_fixed_line_turn_is_not_the_callers_line():
    assert mouth.team_line(f"{mouth.LINE}\nSay only \"Let me check.\" and nothing else.")
    assert not mouth.team_line("Let me check.")
