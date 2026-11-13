"""Shared FastAPI deps for the Teams BFF routers.

``TeamsContext`` (the caller's user, org and owner ids) and ``envelope_list``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def envelope_list(upstream: Any, key: str) -> dict[str, list[dict]]:
    """Wrap an upstream list response in a ``{key: [...]}`` envelope.

    Some upstream reads return bare JSON arrays. The Teams frontend
    panels read ``data?.<resource>`` (envelope shape) so without
    server-side wrapping each panel silently renders empty.

    Tolerates either upstream shape — bare list, or a dict with the
    expected key / a generic ``items`` key — so a future upstream
    change to envelope shape won't 5xx the panel.

    Drops non-dict rows (defensive: some upstream paths can splice in
    error markers).
    """
    if isinstance(upstream, list):
        rows = upstream
    elif isinstance(upstream, dict):
        rows = upstream.get(key) or upstream.get("items") or []
    else:
        rows = []
    return {key: [row for row in rows if isinstance(row, dict)]}


@dataclass
class TeamsContext:
    user_id: str
    org_id: str | None
    owner_id: str
