"""All GPT-Live voice surfaces use the shared English policy; Meet retains per-person English STT."""

import pytest

from huddle_agent import meet, mouth, owner, phone

INFO = {
    "assistantName": "Nova",
    "businessName": "The Lightbulb Company",
    "greeting": "Hi",
    "today": "Sunday",
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "k")


def test_meet_stt_transcribes_english():
    assert meet.STT_LANGUAGE == "en"


def test_every_voice_replies_in_english():
    texts = (
        phone.prompt(INFO),
        owner.prompt(),
        meet.prompt({"name": "Nova", "company": "C", "title": "T", "today": "d"}),
    )
    for text in texts:
        assert mouth.ENGLISH in text


def test_no_hand_off_number_means_no_person_tool_and_a_plain_fact():
    none, some = phone.prompt(INFO), phone.prompt({**INFO, "transfer": True})
    assert "hand the call to a person" not in none and "No person is available" in none
    assert "hand the call to a person" in some and "No person is available" not in some
