import os

os.environ.setdefault("LIGHTBULB_API_URL", "http://x")
os.environ.setdefault("LIGHTBULB_HUDDLE_SECRET", "x")

from huddle_agent import phone  # noqa: E402


def test_the_prompt_reads_back_before_booking_and_offers_three_slots():
    text = " ".join(phone.INSTRUCTIONS.split())
    assert (
        "read back the service, day, time and name, and wait for the caller to say yes"
        in text
    )
    assert "offer up to three open slots" in text
