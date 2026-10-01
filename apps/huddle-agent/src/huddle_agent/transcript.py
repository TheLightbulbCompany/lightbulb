"""Rolling huddle transcript (in memory only) and CEO name detection."""

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


def summons_ceo(text: str, ceo_name: str) -> bool:
    t = " ".join(text.lower().split())
    for name in (ceo_name, "ceo", "team"):
        n = re.escape(" ".join(name.lower().split())).replace("\\ ", " ")
        if not n:
            continue
        if re.match(_LEAD + n + r"(?!\w)", t) or re.search(_VERB + n + r"(?!\w)", t):
            return True
    return False
