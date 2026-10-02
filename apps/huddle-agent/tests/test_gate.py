from huddle_agent import gate
from huddle_agent.gate import FOLLOWUP, NEWS, RateLimit, candidate

PEOPLE = ["Aden Smith", "Leo"]


def c(text, followup_ok=True, news=False, exchange_open=False):
    return candidate(text, others=PEOPLE, exchange_open=exchange_open, followup_ok=followup_ok, news=news)


def test_filter_skips_backchannels():
    for line in ("yeah", "mm-hmm", "ok thanks Lightbulb", "got it, makes sense", "Right.", "sounds good"):
        assert c(line, news=True) is None, line


def test_filter_skips_lines_addressed_to_a_person():
    for line in ("Aden, can you check that?", "what do you think, Leo?", "hey leo what's next", "@Aden thoughts?"):
        assert c(line, news=True) is None, line


def test_filter_skips_mid_thought_pauses():
    for line in ("so the thing about Q3 is,", "and then we", "I was thinking that maybe...", "we could try the"):
        assert c(line, news=True) is None, line


def test_filter_skips_mentions():
    assert c("Lightbulb said that earlier", news=True) is None


def test_kinds():
    assert c("and what about Q3?") == FOLLOWUP
    assert c("and what about Q3?", followup_ok=False) is None  # an open question alone never gates
    assert c("the deploy went fine.", followup_ok=False, news=True) == NEWS
    assert c(None, followup_ok=False, news=True) == NEWS  # news landed in a lull
    assert c("the deploy went fine.", followup_ok=False, news=True, exchange_open=True) is None


def test_rate_limit_one_per_90s_and_ten_per_hour():
    r = RateLimit()
    t = 0.0
    assert r.ok(t)
    r.record(t)
    assert not r.ok(t + gate.MIN_GAP - 1) and r.ok(t + gate.MIN_GAP)
    for i in range(1, 10):
        r.record(t + i * gate.MIN_GAP)
    assert not r.ok(t + 10 * gate.MIN_GAP)  # ten in the last hour
    assert r.ok(t + 3600)  # the first one aged out
