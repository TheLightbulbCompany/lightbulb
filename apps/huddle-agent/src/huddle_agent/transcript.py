"""Rolling huddle transcript (in memory only) and the "Lightbulb" wake word."""

from __future__ import annotations

import re

# Ported from apps/backend/core/services/voice_routing.py (named_agent).
_LEAD = r"^(?:(?:hey|ok|okay|so)[\s,]+)?"
_VERB = r"\b(?:ask|tell|get|have)\s+"


class Transcript:
    def __init__(self, max_chars: int = 6000):
        self.max_chars = max_chars
        self._lines: list[str] = []
        self._dropped = 0  # lines trimmed so far; keeps marks stable across trims

    def add(self, name: str, text: str) -> None:
        self._lines.append(f"{name}: {text}")
        while len(self._lines) > 1 and len("\n".join(self._lines)) > self.max_chars:
            self._lines.pop(0)
            self._dropped += 1

    def text(self) -> str:
        return "\n".join(self._lines)

    def since(self, mark: int) -> tuple[str, int]:
        end = self._dropped + len(self._lines)
        return "\n".join(self._lines[max(mark - self._dropped, 0):]), end


# STT writes the wake word as "Lightbulb", "light bulb" or "light-bulb"; punctuation after it is fine.
_WAKE = r"light[\s-]?bulb(?!\w)"


def summons_lightbulb(text: str) -> bool:
    """Only "Lightbulb" wakes the voice: at the start (after an optional hey/ok/so), or after ask/tell/get/have."""
    t = " ".join(text.lower().split())
    return bool(re.match(_LEAD + _WAKE, t) or re.search(_VERB + _WAKE, t))


# Backchannels and fillers: never enough to cut Lightbulb off. Multi-word ones go first.
_FILLER_PHRASES = re.compile(r"\b(?:got it|mm[\s-]?hmm|uh[\s-]?huh)\b")
_FILLER = {"yeah", "yep", "yes", "mm", "mhm", "hmm", "right", "ok", "okay", "sure", "cool", "nice", "true", "uh", "um"}
_HALT = re.compile(r"\b(?:stop|wait|hold on|hang on|one sec)\b")


def real_speech(text: str) -> bool:
    """Enough to talk over Lightbulb: a halt word, or two or more words that are not backchannel."""
    t = text.lower()
    if _HALT.search(t):
        return True
    words = re.findall(r"[a-z0-9']+", _FILLER_PHRASES.sub(" ", t))
    return sum(w not in _FILLER for w in words) >= 2


def addressed_by_name(said: str, people: dict[str, str]) -> str | None:
    """Who Lightbulb's last sentence asks by name ("..., John, does that work?" / "John, can you confirm?")."""
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", said.strip()) if s]
    if not sentences:
        return None
    last = sentences[-1]
    best: tuple[int, str] | None = None
    for identity, display in people.items():
        for name in {display, display.split()[0]} if display.strip() else ():
            word = rf"(?<!\w){re.escape(name)}(?!\w)"
            if last.endswith("?"):
                m = None
                for m in re.finditer(word, last, re.IGNORECASE):
                    pass  # the last mention is the one being asked
                if m and (best is None or m.start() > best[0]):
                    best = (m.start(), identity)
            elif re.match(word + r"\s*,", last, re.IGNORECASE) and best is None:
                best = (0, identity)
    return best[1] if best else None
