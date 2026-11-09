"""Shared speech formatting, English language policy, and meeting context.

GPT-Live owns conversation and delegation; these helpers only format existing room content.
"""

from __future__ import annotations

import re

ANSWER_WITHIN_S = 60.0  # a line delegated this soon after a Worker's question was said answers it; later, a new ask


SPOKEN_CHARS = (
    600  # what the talker is handed of one message; the whole message is in the chat
)


_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


_URL = re.compile(r"https?://\S+")


_MARKS = re.compile(r"[*_`#>|~]+")


_CHOICE_BLOCK = re.compile(r"```openui-lang\s.*?```", re.S)


_CHOICE = re.compile(r"Choice\(\s*\"(?:[^\"\\]|\\.)*\"\s*,\s*\[([^\]]*)\]")


def _spoken_choice(m: re.Match) -> str:
    said = []
    for c in _CHOICE.finditer(m.group(0)):
        opts = re.findall(r'"((?:[^"\\]|\\.)*)"', c.group(1))
        if opts:
            said.append(
                opts[0] if len(opts) == 1 else ", ".join(opts[:-1]) + " or " + opts[-1]
            )
        # ponytail: Choice is the only block a caller-thread question has used; others stay unsaid
    return " " + ". ".join(said) + ("." if said else "") + " "


def speakable(text: str) -> str:
    """The message as the talker is handed it: links as their label, URLs and markdown marks dropped, capped."""
    text = _CHOICE_BLOCK.sub(_spoken_choice, text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = _MARKS.sub("", _URL.sub("", _LINK.sub(r"\1", text)))
    text = " ".join(
        re.sub(r"^\s*[-+]\s+|^\s*\d+\.\s+", "", line) for line in text.splitlines()
    )
    return " ".join(text.split())[:SPOKEN_CHARS]


LANGUAGE = "en"


ENGLISH = "Speak only English, whatever language a line seems to be in."


class Context:
    """The talk's turns since the last delegation (Pipecat's "transcript turns since the previous delegation"): the
    Router gets them word for word with the request."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []

    def add(self, who: str, text: str) -> None:
        if text := " ".join(text.split()):
            self.turns.append((who, text))

    def take(self, request: str) -> str:
        turns, self.turns = self.turns, []
        said = [f"> {who}: {text}" for who, text in turns if text != request.strip()]
        return request.strip() + (
            "\n\nSaid in the talk since the last ask:\n" + "\n".join(said)
            if said
            else ""
        )
