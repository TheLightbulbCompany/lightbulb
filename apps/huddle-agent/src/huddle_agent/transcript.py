"""Rolling huddle transcript (in memory only) and the "Lightbulb" wake word."""

from __future__ import annotations

import re

# Ported from apps/backend/core/services/voice_routing.py (named_agent).
_LEAD = r"^(?:(?:hey|ok|okay|so)[\s,]+)?"
_VERB = r"\b(?:ask|tell|get|have)\s+"


class Transcript:
    def __init__(self, max_chars: int = 6000):
        self.max_chars = max_chars
        self._lines: list[tuple[str, str]] = []  # (name, text)
        self._dropped = 0  # lines trimmed so far; keeps marks stable across trims

    def add(self, name: str, text: str) -> None:
        self._lines.append((name, text))
        while len(self._lines) > 1 and len(self.text()) > self.max_chars:
            self._lines.pop(0)
            self._dropped += 1

    def text(self) -> str:
        return _render(self._lines)

    def since(self, mark: int) -> tuple[str, int]:
        end = self._dropped + len(self._lines)
        return _render(self._lines[max(mark - self._dropped, 0) :]), end

    def tail(self, max_chars: int) -> list[tuple[str, str]]:
        """The newest lines whose text fits in max_chars (at least the last one, clipped)."""
        out: list[tuple[str, str]] = []
        used = 0
        for name, text in reversed(self._lines):
            if out and used + len(text) > max_chars:
                break
            out.append((name, text[-max_chars:]))
            used += len(text)
        return out[::-1]


def _render(lines: list[tuple[str, str]]) -> str:
    return "\n".join(f"{name}: {text}" for name, text in lines)


# STT writes the wake word as "Lightbulb", "light bulb" or "light-bulb"; punctuation after it is fine.
_WAKE = r"light[\s-]?bulb(?!\w)"


def summons_lightbulb(text: str) -> bool:
    """Only "Lightbulb" wakes the voice: at the start (after an optional hey/ok/so), or after ask/tell/get/have."""
    t = " ".join(text.lower().split())
    return bool(re.match(_LEAD + _WAKE, t) or re.search(_VERB + _WAKE, t))
