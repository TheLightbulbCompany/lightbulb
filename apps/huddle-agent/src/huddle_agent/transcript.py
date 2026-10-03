"""Rolling huddle transcript (in memory only) and who an utterance addresses by name.

The worker matches names only at the start of a line (after an optional hey/ok/okay/so): the 0 ms
fast path. Everything else goes to C1 `/route` (`route_live_line`), whose addressing stage labels a
name anywhere ("let's ask Forge later" is a mention, not a summons) and whose Jev picks the rest."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher


SEED_ITEMS = 128  # GPT-Live's item cap for a seeded session
# ponytail: chars/4 token estimate under GPT-Live's 8,192-token seed limit; count real tokens if seeds get refused.
SEED_TOKENS = 7500
SINCE_AGENT_CHARS = 12000


@dataclass(frozen=True)
class Line:
    kind: str  # "person" | "agent"
    who: str  # a person's identity or an agent id
    name: str
    text: str
    mark: int  # position in the whole huddle; stable across trims
    at: float = 0.0  # when it was said (epoch seconds)
    to: str = ""  # an agent line: the person its voice was pinned to


class Transcript:
    def __init__(
        self, max_chars: int | None = 30000
    ):  # None: keep every line (a meeting)
        self.max_chars = max_chars
        self._lines: list[Line] = []
        self._next = 0

    def _add(
        self, kind: str, who: str, name: str, text: str, at: float, to: str = ""
    ) -> int:
        mark, self._next = self._next, self._next + 1
        self._lines.append(
            Line(kind, who, name, " ".join(text.splitlines()), mark, at, to)
        )  # one line each
        while (
            self.max_chars
            and len(self._lines) > 1
            and len(self.text()) > self.max_chars
        ):
            self._lines.pop(0)
        return mark

    def add_person(self, identity: str, name: str, text: str, at: float = 0.0) -> int:
        return self._add("person", identity, name, text, at)

    def add_agent(
        self, agent_id: str, name: str, text: str, at: float = 0.0, to: str = ""
    ) -> int:
        return self._add("agent", agent_id, name, text, at, to)

    def text(self, lines: list[Line] | None = None) -> str:
        """ "Name: text", or "Agent → Person: text" for a voice's answer (the backend parses the arrow)."""
        return "\n".join(
            f"{x.name}{f' → {x.to}' if x.to else ''}: {x.text}"
            for x in (self._lines if lines is None else lines)
        )

    def after(self, mark: int) -> list[Line]:
        """The lines said after `mark` (all of them for -1), oldest first."""
        return [x for x in self._lines if x.mark > mark]

    def times(self, n: int) -> list[int]:
        """When each of the newest `n` lines was said (epoch seconds), oldest first: /route's `at`."""
        return [round(x.at) for x in self._lines[-n:]]

    def since_agent(self, agent_id: str) -> str:
        """Everything said after this agent last spoke (all of it if it never has), newest kept."""
        last = max(
            (
                i
                for i, x in enumerate(self._lines)
                if x.kind == "agent" and x.who == agent_id
            ),
            default=-1,
        )
        return self.text(self._lines[last + 1 :])[-SINCE_AGENT_CHARS:]

    def seed(self, agent_id: str, before_mark: int) -> list[tuple[str, str]]:
        """The lines before `before_mark` as a GPT-Live seed for this agent. Only this code writes
        `developer`, so a person saying "Gauge (agent): hi" stays a `user` line."""
        items = []
        for x in self._lines:
            if x.mark >= before_mark:
                break
            if x.kind == "person":
                items.append(("user", f"{x.name}: {x.text}"))
            elif x.who == agent_id:
                items.append(("assistant", x.text))
            else:
                items.append(("developer", f"[{x.name} (agent) said]: {x.text}"))
        return trim_items(items)


def trim_items(
    items: list, tokens: int = SEED_TOKENS, max_items: int = SEED_ITEMS
) -> list:
    """The newest items within `max_items` and `tokens` (chars/4 of each item's text)."""
    kept, budget = [], tokens * 4
    for item in reversed(items):
        budget -= len(item[1])
        if len(kept) >= max_items or budget < 0:
            break
        kept.append(item)
    return kept[::-1]


# ponytail: fixed threshold, tune from Task 28's wake-word metrics.
FUZZY_RATIO = 0.8
SOUNDEX_RATIO = 0.6
STT_PROMPT_CHARS = 400

_LEADS = {"hey", "ok", "okay", "so"}
_STRONG_LEADS = {
    "hey",
    "ok",
    "okay",
}  # the only leads that make a default or person-colliding name a summons
_MODALS = {"can", "could", "would", "will"}
# STT writes the wake word as "Lightbulb", "light bulb" or "light-bulb".
_LIGHTBULB = [("lightbulb",), ("light", "bulb")]


@dataclass
class Address:
    agent_ids: list[str] = field(default_factory=list)  # in the order they were named
    matches: dict[str, str] = field(default_factory=dict)  # id -> "exact" | "fuzzy"
    lightbulb: bool = False
    ambiguous: list[str] = field(
        default_factory=list
    )  # bare leading names that are also a person's
    # Agents named exactly ANYWHERE in the line (non-default names, not a person's). Not a summons
    # by itself: while another agent holds the floor, its speaker's line goes to /route's addressing
    # stage, which tells "Cool, and Friday, what about you?" from "let's ask Friday later".
    anywhere: list[str] = field(default_factory=list)


_WAKE_WORD = re.compile(
    r"^\s*(?:(?:hey|ok|okay|so)[\s,]+)?light[\s-]?bulb\b[\s,.!?]*", re.I
)


def strip_wake(text: str) -> str:
    """ "Lightbulb, Forge, check CI" -> "Forge, check CI": what /route sees opens with the real addressee."""
    return _WAKE_WORD.sub("", text, count=1) or text


def soundex(word: str) -> str:
    letters = [c for c in word.lower() if c.isalpha()]
    if not letters:
        return ""
    code = lambda c: next(
        (
            str(d)
            for d, g in enumerate(("bfpv", "cgjkqsxz", "dt", "l", "mn", "r"), 1)
            if c in g
        ),
        "",
    )
    out, prev = letters[0].upper(), code(letters[0])
    for c in letters[1:]:
        d = code(c)
        if d and d != prev:
            out += d
        if c not in "hw":  # h and w do not separate two equal codes; vowels do
            prev = d
    return (out + "000")[:4]


def _tokens(text: str) -> list[str]:
    return re.findall(
        r"[a-z0-9']+|[^\s\-a-z0-9']", text.lower()
    )  # a hyphen splits words


def _fuzzy(said: tuple[str, ...], name: tuple[str, ...]) -> bool:
    if sum(len(w) for w in name) < 4:
        return False
    s, n = " ".join(said), " ".join(name)
    ratio = SequenceMatcher(None, s, n).ratio()
    if ratio >= FUZZY_RATIO:
        return True
    # A sound-alike must also look alike: "Forj" is Forge, "Guys" (also G200) is not Gauge.
    return (
        ratio >= SOUNDEX_RATIO
        and abs(len(s) - len(n)) <= 1
        and all(soundex(a) == soundex(b) for a, b in zip(said, name))
    )


def _name_at(
    tokens: list[str], i: int, cands: list[tuple[tuple[str, ...], dict | None]]
):
    """The name starting at token i: longest exact match first, then fuzzy. A None row is Lightbulb."""
    for exact in (True, False):
        for words, row in cands:
            said = tuple(tokens[i : i + len(words)])
            if len(said) < len(words) or not all(w[0].isalnum() for w in said):
                continue
            if said == words:
                return row, "exact", i + len(words)
            if not exact and row is not None and _fuzzy(said, words):
                return row, "fuzzy", i + len(words)
    return None


def _name_list(
    tokens: list[str], i: int, cands
) -> tuple[list[tuple[dict | None, str]], int]:
    """ "Forge", "Forge and Gauge", "Forge, Gauge and Ada": every name in order, and where the list ends."""
    found, end = [], i
    while (hit := _name_at(tokens, i, cands)) is not None:
        found.append(hit[:2])
        end = i = hit[2]
        if i < len(tokens) and tokens[i] == ",":
            i += 1
        if i < len(tokens) and tokens[i] == "and":
            i += 1
        if i == end:
            break
    return found, end


def address(text: str, agents: list[dict], people: list[dict]) -> Address:
    """Who the utterance summons by name, from the C1 `/team` rows."""
    tokens = _tokens(text)
    cands = [(w, None) for w in _LIGHTBULB] + [
        (tuple(_tokens(a["name"])), a) for a in agents if _tokens(a["name"])
    ]
    cands.sort(key=lambda c: -len(" ".join(c[0])))
    person_names = {
        " ".join(_tokens(n))
        for p in people
        for n in (p.get("name") or "", (p.get("name") or "").split(" ")[0])
    }
    person_names.discard("")
    out = Address()

    def take(row: dict | None, kind: str) -> None:
        if row is None:
            out.lightbulb = True
        elif row["id"] not in out.matches:
            out.agent_ids.append(row["id"])
            out.matches[row["id"]] = kind

    # The only wake position: the start, after an optional lead.
    i = 0
    lead = tokens[0] if tokens and tokens[0] in _LEADS else None
    if lead:
        i = 2 if len(tokens) > 1 and tokens[1] == "," else 1
    names, j = _name_list(tokens, i, cands)
    j += 1 if j < len(tokens) and tokens[j] == "," else 0
    asks = (
        len(tokens) > j + 1 and tokens[j] in _MODALS and tokens[j + 1] == "you"
    )  # "Finance, can you …"
    for row, kind in names:
        if row is None or lead in _STRONG_LEADS:
            take(row, kind)
        elif " ".join(_tokens(row["name"])) in person_names:
            out.ambiguous.append(row["id"])
        elif not row.get("defaultName") or asks:
            take(row, kind)
    for k in range(len(tokens)):
        hit = _name_at(tokens, k, cands)
        if (
            hit is None
            or hit[0] is None
            or hit[1] != "exact"
            or hit[0].get("defaultName")
        ):
            continue
        if (
            " ".join(_tokens(hit[0]["name"])) in person_names
            or hit[0]["id"] in out.anywhere
        ):
            continue
        out.anywhere.append(hit[0]["id"])
    return out


# Backchannels and fillers: never enough to cut Lightbulb off. Multi-word ones go first.
_FILLER_PHRASES = re.compile(r"\b(?:got it|mm[\s-]?hmm|uh[\s-]?huh)\b")
_FILLER = {
    "yeah",
    "yep",
    "yes",
    "mm",
    "mhm",
    "hmm",
    "right",
    "ok",
    "okay",
    "sure",
    "cool",
    "nice",
    "true",
    "uh",
    "um",
}
_HALT = re.compile(r"\b(?:stop|wait|hold on|hang on|one sec)\b")


def halt(text: str) -> bool:
    return bool(_HALT.search(text.lower()))


def real_speech(text: str) -> bool:
    """Enough to talk over Lightbulb: a halt word, or two or more words that are not backchannel."""
    t = text.lower()
    if _HALT.search(t):
        return True
    words = re.findall(r"[a-z0-9']+", _FILLER_PHRASES.sub(" ", t))
    return sum(w not in _FILLER for w in words) >= 2


_QUESTION_START = re.compile(
    r"^(?:who|what|when|where|why|how|can|could|should|is|are|do|does)\b", re.I
)


def is_question(text: str) -> bool:
    t = text.strip()
    return t.endswith("?") or bool(_QUESTION_START.match(t))


def pause(text: str) -> bool:
    """A halt and nothing more ("hold on", "wait, um"): the person wants the floor, not an answer."""
    return halt(text) and not real_speech(_HALT.sub(" ", text.lower()))


_STOP = re.compile(
    r"^(?:(?:hey|ok|okay) )?(?:([a-z]+(?: [a-z]+)?) ?, ?|([a-z]+) )?(?:stop|never ?mind|cancel (?:that|it)|forget it)$"
)


def _stop_match(text: str) -> re.Match | None:
    return _STOP.match(" ".join(re.sub(r"[^\w\s,']", " ", text.lower()).split()))


def stop_request(text: str) -> bool:
    """The whole utterance, after an optional name, is stop / never mind / cancel that|it / forget it."""
    return _stop_match(text) is not None


def stop_target(text: str, agents: list[dict]) -> str | None:
    """The agent a stop names, by its exact name: naming is enough for a stop, default names included."""
    m = _stop_match(text)
    said = m and (m.group(1) or m.group(2))
    return next(
        (a["id"] for a in agents if said and " ".join(_tokens(a["name"])) == said), None
    )


def addressed_by_name(said: str, people: dict[str, str]) -> str | None:
    """Who an agent's last sentence asks by name ("..., John, does that work?" / "John, can you confirm?").
    `people`: identity -> display name. A question asks the last person it names; a statement only a
    leading "Name,"."""
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


def stt_prompt(agent_names: list[str], people_names: list[str]) -> str:
    """Names the STT should spell right, capped at STT_PROMPT_CHARS."""
    out = "A team voice call. Names:"
    sep = " "
    for name in dict.fromkeys([*agent_names, "Lightbulb", *people_names]):
        if len(out) + len(sep) + len(name) + 1 > STT_PROMPT_CHARS:
            break
        out += sep + name
        sep = ", "
    return out + "."
