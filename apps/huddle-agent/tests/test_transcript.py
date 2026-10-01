from huddle_agent.transcript import Transcript, summons_ceo


def test_summons():
    assert summons_ceo("CEO, what did we ship?", "Friday")
    assert summons_ceo("hey Friday can you look", "Friday")
    assert summons_ceo("can you ask team to look", "Friday")
    assert summons_ceo("Team, quick one", "Friday")
    assert not summons_ceo("I asked the CEO yesterday", "Friday")
    assert not summons_ceo("ceofoo", "Friday")


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
