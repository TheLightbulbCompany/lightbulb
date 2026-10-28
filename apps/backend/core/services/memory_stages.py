"""Pure stages of memory recall: rank fusion, page composition, time windows.

No AWS and no I/O; the recall function calls these.
"""

import calendar
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from dateutil import parser as dateparser

_CURRENT_KINDS = {"claim", "observation", "rule", "anchor", "mention"}
_SPEAKER = re.compile(r"^([A-Z][\w'.-]*(?: [A-Z][\w'.-]*)?): (?=\S)")
_SINCE = re.compile(r"^(?P<claim>.*?) \(since (?P<since>[^;()]*)(?P<tail>;.*)?\)$", re.S)
_EXCERPT_CHARS = 280


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


def count_tokens(text: str) -> int:
    # ponytail: tiktoken is not a backend dependency; swap this one function if it becomes one.
    return len(text) // 4


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


def from_record(summary: dict, kind_default: str) -> Candidate:
    metadata = summary.get("metadata") or {}
    text = (summary.get("content") or {}).get("text", "")
    speaker_match = _SPEAKER.match(text)
    valid_to = _when(_meta(metadata, "validTo"))
    status = _meta(metadata, "status") or "current"
    if valid_to and status == "current":
        status = "replaced"
    record_id = summary.get("memoryRecordId", "")
    return Candidate(
        id=record_id,
        kind=_meta(metadata, "kind") or kind_default,
        content=text,
        occurred_at=_when(summary.get("timestamp")) or datetime.fromtimestamp(0, UTC),
        status=status,
        speaker=speaker_match.group(1) if speaker_match else "",
        entity=_meta(metadata, "entity"),
        attribute=_meta(metadata, "attribute"),
        valid_from=_when(_meta(metadata, "validFrom")),
        valid_to=valid_to,
        source_id=record_id,
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
    claim = _SPEAKER.sub("", claim, count=1) if c.speaker else claim
    subject, _, value = claim.partition(": ")
    key = f"{c.entity} {c.attribute}" if c.entity and c.attribute else subject
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
    return {
        "kind": c.kind,
        "content": c.content,
        "occurredAt": c.occurred_at.isoformat(),
        "status": c.status,
        "history": history,
        "sources": [
            {
                "sourceId": c.source_id,
                "occurredAt": c.occurred_at.isoformat(),
                "speaker": c.speaker,
                "excerpt": c.content[:_EXCERPT_CHARS],
            }
        ],
    }


def _stamp_tokens(page: dict) -> None:
    for _ in range(4):  # the count includes its own digits; settles in a pass or two
        n = count_tokens(json.dumps(page))
        if page["usedEvidenceTokens"] == n:
            return
        page["usedEvidenceTokens"] = n


def compose(candidates: list[Candidate], budget_tokens: int, as_of: datetime) -> dict:
    marked = [c for c in mark_contested(candidates) if c.status != "replaced"]

    def group(c: Candidate) -> int:
        if c.status == "contested":
            return 0
        return 1 if c.kind in _CURRENT_KINDS else 2

    marked.sort(key=lambda c: (group(c), -c.fused))  # stable: ties keep input order
    page: dict = {"asOf": as_of.isoformat(), "usedEvidenceTokens": 0, "results": []}
    for c in marked:
        page["results"].append({"rank": len(page["results"]) + 1, **_result(c)})
        _stamp_tokens(page)
        if page["usedEvidenceTokens"] > budget_tokens:
            page["results"].pop()
            break
    _stamp_tokens(page)
    return page


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
