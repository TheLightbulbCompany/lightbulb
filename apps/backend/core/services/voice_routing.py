"""Who handles a spoken request: a name you say → Jev → CEO (spec 2026-10-01 §4)."""

from __future__ import annotations

import re

_LEAD = r"^(?:(?:hey|ok|okay|so)[\s,]+)?"
_VERB = r"\b(?:ask|tell|get|have)\s+"


def _pattern(name: str) -> str:
    return re.escape(" ".join(name.lower().split())).replace("\\ ", " ")


def named_agent(text: str, team: list[dict]) -> str | None:
    t = " ".join(text.lower().split())
    for m in sorted(team, key=lambda m: -len(m["name"])):  # longest name wins ("Customer Operations" over "Customer")
        name = _pattern(m["name"])
        if re.match(_LEAD + name + r"(?!\w)", t) or re.search(_VERB + name + r"(?!\w)", t):
            return m["id"]
    return None


def mentioned_agent(text: str, team: list[dict]) -> str | None:
    """The first agent named anywhere in the request ("Lightbulb, what did Gauge find?" -> Gauge).
    Only /route uses it: the worker found no name in a wake position, so any name inside wins."""
    # ponytail: a common-word default name ("growth") inside the request routes to that agent; strict
    # forms like the worker's default-name rule if that misroutes in practice.
    t = " ".join(text.lower().split())
    hits = [
        (hit.start(), -len(m["name"]), m["id"])
        for m in team
        if (hit := re.search(r"(?<!\w)" + _pattern(m["name"]) + r"(?!\w)", t))
    ]
    return min(hits)[2] if hits else None
