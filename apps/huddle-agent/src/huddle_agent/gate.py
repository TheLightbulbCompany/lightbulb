"""When may Lightbulb speak unprompted? The deterministic half: no network, no LiveKit.

Two kinds only. NEWS: an agent result that landed while no exchange was open. FOLLOWUP: a
person's line within FOLLOWUP_SECONDS of Lightbulb speaking. Everything else -- open questions
to the room, mentions, lines addressed to a person, backchannels, mid-thought pauses -- never
reaches the backend gate (apps/backend/core/services/speak_gate.py).
"""

from __future__ import annotations

import re
from collections import deque

PAUSE_S = 0.6  # the whole room silent this long after an STT final
SPEAK_P = 0.85  # the bench's ship threshold (speak_gate_bench/results/2026-10-01-news.md)
HAND_P = 0.6
HAND_SECONDS = 15
FOLLOWUP_SECONDS = 20
NEWS_TTL = 600
MAX_NEWS = 5
MIN_GAP = 90  # proactive speaks per room
PER_HOUR = 10

NEWS = "news"
FOLLOWUP = "followup"

_WORD = re.compile(r"[a-z0-9']+")
_BACKCHANNEL = {
    "yeah", "yep", "yes", "yup", "no", "nope", "mm", "mhm", "hmm", "mmhmm", "uh", "huh", "um", "ah", "oh",
    "right", "ok", "okay", "sure", "cool", "nice", "true", "great", "perfect", "awesome", "good", "fine",
    "got", "it", "thanks", "thank", "you", "cheers", "lightbulb", "light", "bulb", "makes", "sense", "totally",
    "exactly", "agreed", "wow", "interesting", "alright", "all", "gotcha", "fair", "enough", "sounds",
}  # fmt: skip
_TRAILING = {
    "and", "so", "but", "or", "because", "um", "uh", "like", "the", "a", "an", "to", "of", "with", "then",
    "if", "my", "our", "we", "i",
}  # fmt: skip
_MENTION = re.compile(r"light[\s-]?bulb(?!\w)")


def backchannel(text: str) -> bool:
    """"yeah", "mm-hmm", "ok thanks Lightbulb", "got it, makes sense": nothing to answer."""
    words = _WORD.findall(text.lower().replace("-", ""))
    return len(words) <= 6 and all(w in _BACKCHANNEL for w in words)


def mid_thought(text: str) -> bool:
    """The speaker paused, they didn't finish: a trailing comma, dash or ellipsis, or a dangling word."""
    t = text.rstrip()
    if t.endswith((",", "-", "—", "...", "…", ":")):
        return True
    words = _WORD.findall(t.lower())
    return bool(words) and words[-1] in _TRAILING and not t.endswith((".", "?", "!"))


def addressed_to_person(text: str, names: list[str]) -> bool:
    """Opens with a present person's name ("Aden, can you…") or closes on one ("…, Aden?")."""
    t = " ".join(text.lower().split())
    for name in {n.split()[0].lower() for n in names if n.strip()}:
        w = re.escape(name)
        if re.match(rf"^(?:(?:hey|ok|okay|so|and)[\s,]+)?@?{w}(?!\w)", t) or re.search(rf"(?<!\w)@?{w}\W*$", t):
            return True
    return False


def mentions_lightbulb(text: str) -> bool:
    return bool(_MENTION.search(text.lower()))


def candidate(
    text: str | None, *, others: list[str], exchange_open: bool, followup_ok: bool, news: bool
) -> str | None:
    """The kind worth asking the gate about, or None. `text` is the latest line (None: news landed in a lull);
    `others` the present people's names other than its speaker. A summon is not here: the wake word
    already opened an exchange, and a mere mention never speaks."""
    if text is not None and (
        backchannel(text) or mid_thought(text) or addressed_to_person(text, others) or mentions_lightbulb(text)
    ):
        return None
    if followup_ok and text is not None:
        return FOLLOWUP
    if news and not exchange_open:
        return NEWS
    return None


class RateLimit:
    def __init__(self, gap: float = MIN_GAP, per_hour: int = PER_HOUR):
        self._gap, self._per_hour = gap, per_hour
        self._at: deque[float] = deque()

    def ok(self, now: float) -> bool:
        while self._at and now - self._at[0] >= 3600:
            self._at.popleft()
        return len(self._at) < self._per_hour and (not self._at or now - self._at[-1] >= self._gap)

    def record(self, now: float) -> None:
        self._at.append(now)
