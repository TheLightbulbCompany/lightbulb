"""Pure stages of memory recall: rank fusion, page composition, time windows.

No AWS and no I/O; the recall function calls these.
"""

import calendar
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from dateutil import parser as dateparser

_CURRENT_KINDS = {"claim", "observation", "rule", "anchor", "mention"}
_SPEAKER = re.compile(r"^([A-Z][\w'.-]*(?: [A-Z][\w'.-]*)?): (?=\S)")
_SINCE = re.compile(r"^(?P<claim>.*?) \(since (?P<since>[^;()]*)(?P<tail>;.*)?\)$", re.S)
_SIDE = re.compile(r"^(?P<name>[^:]+): (?P<claim>.*) \((?P<who>[^()]*?), (?P<date>[^()]*)\)$", re.S)
_SPEAKER_KINDS = _CURRENT_KINDS | {"turn"}
_LOG_KINDS = {"fact", "decision", "lesson", "preference"}  # the network log's kinds (memory_sync: logKind)
# A question's page, as the CLI's: up to 50 turns and 50 of the rest, each turn the 800-character
# window that matches the question best.
PAGE_TURNS = 50
PAGE_OTHERS = 50
TURN_WINDOW = 800
_STOPWORDS = frozenset(  # the CLI's (lightbulb-network src/memory.rs STOPWORDS)
    "how do does we i you the a an of to in on is are was were be it that this what when where which "
    "who why before after with for and or not our my your".split()
)


@dataclass
class Candidate:
    id: str
    kind: str
    content: str
    occurred_at: datetime
    status: str = "current"
    speaker: str = ""
    entity: str = ""
    attribute: str = ""
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    source_id: str = ""
    fused: float = 0.0
    task_id: str = ""  # super-agent tags (room ingestion, Worker turns); empty elsewhere
    thread_id: str = ""


def count_tokens(text: str) -> int:
    # ponytail: tiktoken is not a backend dependency; swap this one function if it becomes one.
    return -(-len(text) // 4)  # characters / 4 rounded up: the benchmark runner's evidence count


def _clean(word: str) -> str:
    return re.sub(r"^\W+|\W+$", "", word).casefold()


def excerpt(text: str, question: str, width: int = TURN_WINDOW) -> str:
    """The ``width``-character window of ``text`` holding the most distinct question terms (3+
    characters, not stopwords, matched as a word prefix), the earliest on a tie, "…" when it starts
    inside. Text that fits is whole; no match is the opening."""
    if len(text) <= width:
        return text
    terms = tuple(dict.fromkeys(t for w in question.split() if len(t := _clean(w)) >= 3 and t not in _STOPWORDS))
    spans = [(m.start(), m.end(), _clean(m[0])) for m in re.finditer(r"\S+", text)]
    hits = [[t for t in terms if w.startswith(t)] if w.startswith(terms) else [] for _, _, w in spans]
    room = width - 1  # one character for the lead
    held: dict[str, int] = {}
    best, best_score, end = 0, 0, 0
    for i, (start, _, _) in enumerate(spans):  # two pointers: the window is spans[i:end]
        end = max(end, i)
        while end < len(spans) and spans[end][1] - start <= room:
            for t in hits[end]:
                held[t] = held.get(t, 0) + 1
            end += 1
        if len(held) > best_score:
            best, best_score = i, len(held)
        for t in hits[i] if end > i else ():
            held[t] -= 1
            if not held[t]:
                del held[t]
    if best == 0:
        return text[:width]
    return "…" + text[spans[best][0] :][:room]


def _meta(metadata: dict, key: str) -> str:
    wrapper = metadata.get(key) or {}
    value = wrapper.get("stringValue", wrapper.get("numberValue", ""))
    return str(value)


def _when(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        if isinstance(value, (int, float)) or str(value).replace(".", "", 1).isdigit():
            return datetime.fromtimestamp(float(value), UTC)
        parsed = dateparser.parse(str(value))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _until(value: str) -> datetime | None:
    """validTo; 0 or below is no end: the extraction model writes 0 for "empty" (a NUMBER field)."""
    try:
        if float(value) <= 0:
            return None
    except ValueError:
        pass
    return _when(value)


def _said_when(valid_from: datetime | None, created: datetime | None, text: str) -> datetime | None:
    """validFrom, unless the extraction model guessed its year: more than a year before the record
    was written while the text names no year that early. Haiku has no calendar of its own and wrote
    2024 for claims said in 2026 (prod, 30 Oct 2026); the record was written when it was said."""
    if not (valid_from and created) or created - valid_from <= timedelta(days=365):
        return valid_from
    if any(int(y) <= valid_from.year for y in _YEAR_RE.findall(text)):
        return valid_from  # dated history ("since 2024-05-01"): the text says so
    return created


def from_record(summary: dict, kind_default: str) -> Candidate:
    metadata = summary.get("metadata") or {}
    text = (summary.get("content") or {}).get("text", "")
    kind = _meta(metadata, "kind") or kind_default
    if _meta(metadata, "logKind") or kind in _LOG_KINDS:
        kind = "entry"  # a mirrored log entry; mirrors written before logKind carried the log kind as kind
    contested = text.startswith("Contested: ")
    speaker = ""
    if contested:
        side = _SIDE.match(text.removeprefix("Contested: ").split(" vs ", 1)[0] + " (x, y)")
        speaker = side["name"] if side else ""
    elif kind in _SPEAKER_KINDS and (m := _SPEAKER.match(text)):
        speaker = m.group(1)
    elif kind == "entry":
        speaker = _meta(metadata, "author")
    valid_to = _until(_meta(metadata, "validTo"))
    status = "contested" if contested else _meta(metadata, "status") or "current"
    if valid_to and status == "current":
        status = "replaced"
    record_id = summary.get("memoryRecordId", "")
    created = _when(summary.get("createdAt"))
    valid_from = _when(_meta(metadata, "validFrom"))
    if kind != "entry":  # a mirrored log entry's validFrom is the log's own when, not the model's
        valid_from = _said_when(valid_from, created, text)
    return Candidate(
        id=record_id,
        kind=kind,
        content=text,
        occurred_at=created or datetime.fromtimestamp(0, UTC),
        status=status,
        speaker=speaker,
        entity=_meta(metadata, "entity") or _meta(metadata, "subject"),
        attribute=_meta(metadata, "attribute"),
        valid_from=valid_from,
        valid_to=valid_to,
        source_id=record_id,
        task_id=_meta(metadata, "taskId"),
        thread_id=_meta(metadata, "threadId"),
    )


def rrf(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + position)
    # dict preserves first appearance and sorted is stable, so ties keep first-seen order
    return sorted(scores.items(), key=lambda pair: -pair[1])


def _parse(text: str) -> tuple[str, list[dict]]:
    """Return (claim text without the since/was tail, history). Never raises."""
    if text.startswith("Contested: "):
        sides = text.removeprefix("Contested: ").split(" vs ", 1)
        first = _SIDE.match(sides[0] + " (x, y)")
        second = _SIDE.match(sides[1]) if len(sides) == 2 else None
        history = []
        if second:
            stamp = _when(second["date"])
            history.append(
                {
                    "value": second["claim"],
                    "speaker": second["name"],
                    "occurredAt": stamp.isoformat() if stamp else second["date"],
                }
            )
        return (first["claim"] if first else sides[0]), history
    match = _SINCE.match(text)
    if not match:
        return text, []
    history = []
    for part in (match["tail"] or "").split("; was ")[1:]:
        pieces = part.rsplit(",", 2)
        if len(pieces) != 3:
            continue
        old, who, until = (p.strip() for p in pieces)
        old = old.split(": ", 1)[-1]
        until = re.sub(r"^until\s+", "", until)
        stamp = _when(until)
        history.append({"value": old, "speaker": who, "occurredAt": stamp.isoformat() if stamp else until})
    return match["claim"], history


def _key_and_value(c: Candidate) -> tuple[str, str]:
    claim, _ = _parse(c.content)
    if c.speaker and claim.startswith(f"{c.speaker}: "):
        claim = claim[len(c.speaker) + 2 :]
    subject, _, value = claim.partition(": ")
    key = f"{c.entity} {c.attribute}" if c.entity else subject
    return " ".join(key.casefold().split()), value.strip().casefold()


def mark_contested(candidates: list[Candidate]) -> list[Candidate]:
    values: dict[str, set[str]] = {}
    for c in candidates:
        if c.status == "replaced" or c.kind in ("turn", "mention"):
            continue
        key, value = _key_and_value(c)
        if value:
            values.setdefault(key, set()).add(value)
    out = []
    for c in candidates:
        if c.status != "replaced" and c.kind not in ("turn", "mention"):
            key, _ = _key_and_value(c)
            if len(values.get(key, ())) > 1:
                c = replace(c, status="contested")
        out.append(c)
    return out


def _result(c: Candidate) -> dict:
    _, history = _parse(c.content)
    tags = {k: v for k, v in (("taskId", c.task_id), ("threadId", c.thread_id)) if v}
    return {
        **tags,
        "kind": c.kind,
        "content": c.content,
        "occurredAt": c.occurred_at.isoformat(),
        "validFrom": c.valid_from.isoformat() if c.valid_from else None,  # when it held; occurredAt is when stored
        "status": c.status,
        "history": history,
        "sources": [
            {
                "sourceId": c.source_id,
                "occurredAt": c.occurred_at.isoformat(),
                "speaker": c.speaker,
            }
        ],
    }


def compose(candidates: list[Candidate], budget_tokens: int, as_of: datetime) -> dict:
    marked = [c for c in mark_contested(candidates) if c.status != "replaced"]

    def group(c: Candidate) -> int:
        if c.status == "contested":
            return 0
        return 1 if c.kind in _CURRENT_KINDS else 2

    marked.sort(key=lambda c: (group(c), -c.fused))  # stable: ties keep input order
    page: dict = {"asOf": as_of.isoformat(), "usedEvidenceTokens": 0, "results": []}
    room = {True: PAGE_TURNS, False: PAGE_OTHERS}
    for c in marked:
        turn = c.kind == "turn"
        if not room[turn]:
            continue
        # Evidence is content, counted as the benchmark runner counts it; the first overflow ends the page.
        cost = count_tokens(c.content)
        if page["usedEvidenceTokens"] + cost > budget_tokens:
            break
        room[turn] -= 1
        page["usedEvidenceTokens"] += cost
        page["results"].append({"rank": len(page["results"]) + 1, **_result(c)})
    return page


def page_text(page: dict) -> str:
    """The composed page as an agent reads it, as the CLI's: a heading per section, a line per
    result with its day (validFrom when it has one, else occurredAt), in rank order; a line's own newlines are spaces, so no line fakes a heading.
    A super-agent page's task links follow as ``Linked:``.
    apps/infra/hermes/plugins/memory/agentcore keeps a copy (_page_text): change both."""
    sections: dict[str, list[str]] = {"Contested:": [], "Current:": [], "Log:": [], "Turns:": []}
    for r in page["results"]:
        day, text = (r.get("validFrom") or r["occurredAt"])[:10], " ".join(r["content"].split())
        if r["kind"] == "turn":
            sections["Turns:"].append(f"{day}, {text}")
        elif r["status"] == "contested":
            sections["Contested:"].append(f"{text.removeprefix('Contested: ')} ({day})")
        else:
            sections["Current:" if r["kind"] in _CURRENT_KINDS else "Log:"].append(f"{text} ({day})")
    sections["Linked:"] = [link_line(lk) for lk in page.get("links") or []]
    return "\n".join(f"{head}\n" + "\n".join(lines) for head, lines in sections.items() if lines) or "Nothing found."


def link_line(link: dict) -> str:
    """One ``Linked:`` line: what the task is now (the live row), and the id to follow."""
    title = " ".join(link["title"].split())
    day = (link.get("updatedAt") or "")[:10]
    head = f"{link['identifier']} {title} — {link['status']}{f' {day}' if day else ''}"
    threads = ", ".join(t["id"] for t in link.get("threads") or [])
    return f"{head} · {link.get('specialist') or '-'} · task:{link['id']}" + (
        f" (threads: {threads})" if threads else ""
    )


_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
_MONTHS.update({m.lower(): i for i, m in enumerate(calendar.month_abbr) if m})
_MONTH_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?(?:\s+(?:\d{1,2},?\s+)?((?:19|20)\d{2}))?",
    re.I,
)
_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def _span(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    return start, end.replace(hour=23, minute=59, second=59, microsecond=999999)


def _month(year: int, month: int) -> tuple[datetime, datetime]:
    last = calendar.monthrange(year, month)[1]
    return _span(datetime(year, month, 1, tzinfo=UTC), datetime(year, month, last, tzinfo=UTC))


def time_window(question: str, as_of: datetime) -> tuple[datetime | None, datetime | None]:
    now = as_of.astimezone(UTC) if as_of.tzinfo else as_of.replace(tzinfo=UTC)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    q = question.lower()
    try:
        if m := _ISO_RE.search(q):
            day = datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=UTC)
            return _span(day, day)
        # ponytail: "may" the verb reads as the month; tighten if it misfires in practice
        if m := _MONTH_RE.search(q):
            month = _MONTHS[m[1].lower()]
            if m[2]:
                return _month(int(m[2]), month)
            return _month(now.year if month <= now.month else now.year - 1, month)
    except ValueError:
        return None, None
    if "yesterday" in q:
        return _span(today - timedelta(days=1), today - timedelta(days=1))
    if "today" in q:
        return _span(today, today)
    if "last week" in q:
        monday = today - timedelta(days=today.weekday() + 7)
        return _span(monday, monday + timedelta(days=6))
    if "last month" in q:
        first = today.replace(day=1) - timedelta(days=1)
        return _month(first.year, first.month)
    if m := _YEAR_RE.search(q):
        year = int(m[1])
        return _span(datetime(year, 1, 1, tzinfo=UTC), datetime(year, 12, 31, tzinfo=UTC))
    return None, None
