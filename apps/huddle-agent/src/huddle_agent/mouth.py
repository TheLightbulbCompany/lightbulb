"""The voice's mouth (one-brain plan, founder ruling 8 Nov 2026, option A): the orb and the meeting say only what
the room's Router and its Workers post in the room, word for word, each message once. No model writes a word here:
`speakable` only strips markdown and, for a long message, keeps its opening and points to the chat."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from huddle_agent.backend import BackendError

logger = logging.getLogger("mouth")
SPOKEN_CHARS = 320  # about 20 seconds of speech; the whole message is in the chat

_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+")
_MARKS = re.compile(r"[*_`#>|~]+")


def speakable(text: str) -> str:
    """The message as it is said: links read as their label, bare URLs and markdown marks dropped. A long one keeps
    its whole sentences up to SPOKEN_CHARS and says where the rest is."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = _MARKS.sub("", _URL.sub("", _LINK.sub(r"\1", text)))
    text = " ".join(re.sub(r"^\s*[-+]\s+|^\s*\d+\.\s+", "", line) for line in text.splitlines())
    text = " ".join(text.split())
    if len(text) <= SPOKEN_CHARS:
        return text
    head = ""
    for sentence in re.split(r"(?<=[.?!])\s+", text):
        if head and len(head) + len(sentence) > SPOKEN_CHARS:
            break
        head = f"{head} {sentence}".strip()
    return f"{head[:SPOKEN_CHARS].rstrip()} The rest is in the chat."


class Mouth:
    """Polls the room's new agent messages (`fetch(after)` -> {"messages": [{id, text}], "after": seq}) and says
    each once, in order, when `ready()` (nobody is talking)."""

    def __init__(self, fetch: Callable[[int], Awaitable[dict]], after: int) -> None:
        self._fetch, self.after = fetch, after
        self.pending: list[str] = []
        self._said: set[str] = set()

    async def poll(self) -> None:
        try:
            out = await self._fetch(self.after)
        except BackendError as e:
            logger.warning("mouth: events failed (%s)", e.code)
            return
        self.after = max(self.after, int(out.get("after") or 0))
        for m in out.get("messages", []):
            if m["id"] not in self._said and (line := speakable(m.get("text") or "")):
                self._said.add(m["id"])
                self.pending.append(line)

    def speak(self, say: Callable[[str], object], ready: bool) -> int:
        """Says everything pending (queued in order) when `ready`; how many lines were said."""
        if not ready or not self.pending:
            return 0
        lines, self.pending = self.pending, []
        for line in lines:
            try:
                say(line)
            except Exception:  # noqa: BLE001 -- a closed session: the line is in the chat anyway
                logger.warning("mouth: line not said", exc_info=True)
        return len(lines)
