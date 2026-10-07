"""Teams BFF — the unified "needs you" attention feed.

Isol8 surfaces two entirely different things a human has to answer, and
until this route existed neither was aggregated anywhere:

* **Board approvals** — listed by ``GET /teams/approvals``.
* **Issue-thread interactions** (``request_confirmation``,
  ``ask_user_questions``, …) — reachable ONLY by opening the one issue they
  hang off, via ``GET /teams/issues/{id}/interactions``.

The consequence was measured on the founder's live board: a board approval
sat 38 hours unanswered because nothing listed it, and the agent finished
60 seconds after it was answered. Upstream already solves this with a
board-only ``/attention`` feed; this proxies it.

SECURITY — board-only + per-user session. Upstream gates the route with
``assertBoard`` (agent actors are 403'd) and scopes every row to
``req.actor.userId``, because per-user dismissal / snooze state is joined
into the feed. It is proxied with the caller's per-user Paperclip session
cookie (``ctx.session_cookie``), NEVER an instance-admin cookie — an admin
cookie would bypass the upstream company scope and reach another tenant's
feed (cross-tenant IDOR), and would resolve dismissals against the wrong
user. Same rule the four interaction-resolution routes follow; see
``interactions.py``.

WHY THE FEED IS HYDRATED HERE. Upstream's ``AttentionItem.detail`` is a
*preview* (``promptExcerpt``, ``questionCount``), which is enough to LIST a
pending decision but not enough to RESOLVE one — the frontend's
``IssueThreadInteractionCard`` needs the full interaction row (its
``payload`` carries the questions / options / draft tasks), and
``ApprovalRow`` needs the approval's ``payload`` to render a contextual
label. Rather than make the client issue a follow-up request per row (an
N+1 whose N is the size of the user's decision backlog), the two hydration
reads happen here and each item is stamped with the row it needs. The
client gets one round trip and can resolve every item inline.

This route is READ-ONLY and is deliberately NOT gated on trial runway: the
spend doors are the resolution routes (accept / reject / respond / cancel
and approve / reject), which are already gated in ``interactions.py`` and
``approvals.py``. Blocking the *list* would hide the very decisions a
blocked owner needs to see.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from . import agents as _agents
from .deps import TeamsContext
from .interactions import _enrich_interaction_actors

router = APIRouter()
_ctx = _agents._ctx
logger = logging.getLogger(__name__)

# Upstream caps a page at 100 and defaults to 50 (ATTENTION_PAGE_MAX_LIMIT /
# ATTENTION_PAGE_DEFAULT_LIMIT in services/attention.ts). Mirrored locally so a
# bad value is a local 422 instead of an upstream 400 — the inbox router sets
# the same precedent, and an upstream error body can leak the company path.
_MAX_LIMIT = 100
_SORT_MODES = ("activity", "decide")

# Hydrating interactions costs one upstream read per DISTINCT issue on the
# page. Real backlogs are single digits; this bounds a pathological feed so a
# 100-row page can never fan out to 100 upstream calls. Items past the cap keep
# ``interaction: None`` and the client link-outs to the thread instead of
# offering inline resolution.
# ponytail: fixed cap + fan-out; add a batch upstream endpoint if feeds ever
# routinely exceed it.
_MAX_HYDRATED_ISSUES = 25


def _items(feed: Any) -> list[dict]:
    """The feed's item rows, tolerating a non-dict / malformed upstream body."""
    if not isinstance(feed, dict):
        return []
    rows = feed.get("items")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _issue_id_of(item: dict) -> Optional[str]:
    """The issue an ``issue_thread_interaction`` item hangs off.

    Upstream stamps it on ``subject.metadata.issueId`` (authoritative — it is
    read straight off the interaction row). ``relatedIssue.id`` is the
    fallback: it is null when the joined issue is missing or not visible.
    """
    subject = item.get("subject")
    if isinstance(subject, dict):
        metadata = subject.get("metadata")
        if isinstance(metadata, dict):
            issue_id = metadata.get("issueId")
            if isinstance(issue_id, str) and issue_id:
                return issue_id
    related = item.get("relatedIssue")
    if isinstance(related, dict):
        issue_id = related.get("id")
        if isinstance(issue_id, str) and issue_id:
            return issue_id
    return None


def _subject_id_of(item: dict) -> Optional[str]:
    subject = item.get("subject")
    if not isinstance(subject, dict):
        return None
    subject_id = subject.get("id")
    return subject_id if isinstance(subject_id, str) and subject_id else None


async def _hydrate_approvals(ctx: TeamsContext, items: list[dict]) -> None:
    """Stamp the full approval row onto each ``approval`` item, in place.

    ONE upstream read for the whole page (``GET /companies/{co}/approvals``
    returns every pending row), joined by id. Best-effort: a failed read or an
    id the list does not carry leaves ``approval`` unset, and the client falls
    back to the feed's own title.
    """
    wanted = {
        subject_id for item in items if item.get("sourceKind") == "approval" and (subject_id := _subject_id_of(item))
    }
    if not wanted:
        return

    rows = await _agents._admin().list_approvals(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
    )
    if isinstance(rows, dict):
        rows = rows.get("approvals") or rows.get("items") or []
    if not isinstance(rows, list):
        return

    by_id = {row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)}
    for item in items:
        if item.get("sourceKind") != "approval":
            continue
        row = by_id.get(_subject_id_of(item))
        if row is not None:
            item["approval"] = row


async def _hydrate_interactions(ctx: TeamsContext, items: list[dict]) -> None:
    """Stamp the full interaction row onto each interaction item, in place.

    One upstream read per DISTINCT issue (deduped — several pending
    interactions on one thread cost a single read), bounded by
    ``_MAX_HYDRATED_ISSUES`` and run concurrently. Best-effort per issue: one
    failing read leaves only that issue's items unhydrated rather than failing
    the feed.
    """
    targets = [
        (item, issue_id)
        for item in items
        if item.get("sourceKind") == "issue_thread_interaction" and (issue_id := _issue_id_of(item))
    ]
    if not targets:
        return

    # Preserve feed order so the cap drops the LEAST urgent issues, not
    # arbitrary ones (the feed is already ranked).
    issue_ids: list[str] = []
    for _, issue_id in targets:
        if issue_id not in issue_ids:
            issue_ids.append(issue_id)
    issue_ids = issue_ids[:_MAX_HYDRATED_ISSUES]

    admin = _agents._admin()

    async def fetch(issue_id: str) -> tuple[str, list]:
        try:
            rows = await admin.list_issue_interactions(
                issue_id=issue_id,
                session_cookie=ctx.session_cookie,
            )
        except Exception:
            logger.exception("attention: interaction hydration failed for issue_id=%s", issue_id)
            return issue_id, []
        if isinstance(rows, dict):
            rows = rows.get("interactions") or rows.get("items") or []
        return issue_id, rows if isinstance(rows, list) else []

    fetched = await asyncio.gather(*(fetch(issue_id) for issue_id in issue_ids))

    by_id: dict[str, dict] = {}
    for _, rows in fetched:
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str):
                by_id[row["id"]] = row

    hydrated: list[dict] = []
    for item, _ in targets:
        row = by_id.get(_subject_id_of(item))
        if row is not None:
            item["interaction"] = row
            hydrated.append(row)

    if hydrated:
        # Same actor stamp the issue thread applies, so a card resolved from
        # this list shows the same names it shows in-thread.
        await _enrich_interaction_actors(ctx, hydrated)


@router.get("/attention")
async def list_attention(
    ctx: TeamsContext = Depends(_ctx),
    limit: Optional[int] = Query(default=None, ge=1, le=_MAX_LIMIT),
    sort: Optional[str] = Query(default=None),
    cursor: Optional[str] = Query(default=None),
    include_dismissed: bool = Query(default=False, alias="includeDismissed"),
):
    """The unified list of decisions waiting on this human.

    Returns upstream's ``AttentionFeed`` verbatim, with two additions per
    item: ``approval`` (the full approval row) and ``interaction`` (the full
    issue-thread interaction row), so the frontend can resolve either kind
    inline without a follow-up request.

    ``totalCount`` and ``countsBySourceKind`` are computed upstream BEFORE
    pagination, so they stay truthful as a badge even when the page is
    capped — which is what lets the UI show a count it did not derive by
    scanning issues client-side (the issue list silently caps ~120 rows,
    isol8 #1992).
    """
    if sort is not None and sort not in _SORT_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"sort must be one of {', '.join(_SORT_MODES)}",
        )

    params: dict[str, Any] = {}
    if limit is not None:
        params["limit"] = str(limit)
    if sort is not None:
        params["sort"] = sort
    if cursor is not None:
        params["cursor"] = cursor
    if include_dismissed:
        params["includeDismissed"] = "true"

    feed = await _agents._admin().list_attention(
        company_id=ctx.company_id,
        session_cookie=ctx.session_cookie,
        params=params or None,
    )
    if not isinstance(feed, dict):
        # Upstream returned something unusable (404 -> {} from the client, or a
        # malformed body). An empty feed is the honest answer — never a
        # fabricated count.
        return {"items": [], "totalCount": 0, "countsBySourceKind": {}}

    items = _items(feed)
    if items:
        # Both hydrations are best-effort decorations over a feed that is
        # already correct; neither may take the list down.
        try:
            await _hydrate_approvals(ctx, items)
        except Exception:
            logger.exception("attention: approval hydration failed; serving feed raw")
        try:
            await _hydrate_interactions(ctx, items)
        except Exception:
            logger.exception("attention: interaction hydration failed; serving feed raw")

    return feed
