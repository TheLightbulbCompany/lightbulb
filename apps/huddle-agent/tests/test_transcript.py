from huddle_agent.transcript import Transcript, summons_lightbulb


def test_wake_word_variants():
    for line in (
        "Lightbulb, what did we ship?",
        "hey lightbulb can you look",
        "Hey, Light bulb. Are you there?",
        "ok light-bulb, status",
        "So Lightbulb what's next",
        "can you ask Lightbulb to check Stripe",
        "tell light bulb to look",
    ):
        assert summons_lightbulb(line), line


def test_only_lightbulb_wakes():
    for line in (
        "CEO, what did we ship?",
        "Team, quick one",
        "hey Ada can you look",
        "I bought a lightbulb yesterday",
        "lightbulbs are cheap",
        "the deploy went fine",
    ):
        assert not summons_lightbulb(line), line


def test_transcript_trims_keeps_newest():
    t = Transcript(max_chars=30)
    for i in range(5):
        t.add("A", f"line{i}")
    assert t.text().endswith("A: line4")
    assert "line0" not in t.text()
    assert len(t.text()) <= 30


def test_since():
    t = Transcript()
    t.add("A", "one")
    text, mark = t.since(0)
    assert text == "A: one"
    t.add("B", "two")
    assert t.since(mark) == ("B: two", mark + 1)
    assert t.since(mark + 1)[0] == ""


def test_since_survives_trim():
    t = Transcript(max_chars=20)
    t.add("A", "one")
    _, mark = t.since(0)
    for x in ("two", "three", "four"):
        t.add("B", x)
    text, _ = t.since(mark)
    assert "one" not in text and "four" in text
