"""Friday's polling wake-up pass.

Runs inside the scheduled-worker loop. Detects when issues Friday
created have changed status (so she can triage) and — eventually —
wakes her with a synthetic session keyed to the changed issue. This
v1 ships the *detection* layer; the wake mechanism itself is a
follow-up that needs the gateway wake-protocol confirmed (see TODO
at the bottom of this module).

Why polling (not events): Paperclip emits ``activity.logged`` and
``heartbeat.run.status`` over the company event WS (see
``paperclip_event_client.py`` and
``docs/superpowers/specs/2026-05-04-teams-realtime-design.md``). The
event-driven wake is the cleaner long-term answer; polling is the
safety net for dropped events (the WS has no backfill on reconnect)
and the v1 starting point while the event wiring lands.

Loop placement: invoked from
``update_service.run_scheduled_worker`` once per outer loop iteration
(every 60s). The pass is gated on an interval so the LLM-light wake
cost stays bounded — _POLL_INTERVAL_SECONDS controls cadence.

See the design spec at
``docs/superpowers/specs/2026-05-13-friday-orchestration-design.md``.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie

logger = logging.getLogger(__name__)


# Pass cadence — runs at most every 5 minutes regardless of how often
# run_scheduled_worker calls into it. Polling every minute would be
# wasteful (most owners have no active work mid-issue). Bumping cadence
# is a one-line change; needs revisit once event-driven wakes land
# and polling moves to pure-safety-net.
_POLL_INTERVAL_SECONDS: float = 300.0


# In-process status cache: {owner_id: {issue_id: last_seen_status}}.
# Stays in memory across pass invocations within a single backend task;
# resets on task restart (acceptable — next poll re-baselines). DDB
# persistence is an optimization deferred until we see real drift.
_STATUS_CACHE: dict[str, dict[str, str]] = {}


async def maybe_run_friday_poll_pass(
    *,
    paperclip_repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
    last_pass_at_holder: list[float],
) -> None:
    """Throttled entry point — runs the pass only if enough time has elapsed.

    ``last_pass_at_holder`` is a single-element list so the caller can
    persist the timestamp across invocations without making this function
    stateful. Mirrors the pattern used by
    ``update_service._paperclip_purge_pass`` for its 24h gating.
    """
    now = time.monotonic()
    last = last_pass_at_holder[0] if last_pass_at_holder else 0.0
    if now - last < _POLL_INTERVAL_SECONDS:
        return
    last_pass_at_holder[0] = now
    try:
        await _friday_poll_pass(
            paperclip_repo=paperclip_repo,
            admin_client=admin_client,
        )
    except Exception:
        # Defense-in-depth: the outer scheduled worker should never crash
        # because Friday polling failed. Log and continue; the heartbeat
        # metric below tells us the pass ran.
        logger.exception("friday_poll: unexpected error in pass")
        put_metric("friday_poll.pass.error")


async def _friday_poll_pass(
    *,
    paperclip_repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> None:
    """One poll pass: enumerate active companies, diff issue status, log changes.

    Per pass:
      1. Scan active Paperclip companies (status="active").
      2. Acquire admin session cookie once for the pass.
      3. For each company, list issues (filtered to ones Friday created
         when that filter is available — see TODO below).
      4. Diff each issue's status against the in-process cache.
      5. On change → log + emit metric. The actual wake-Friday call is
         a TODO at the bottom of this loop.
    """
    put_metric("friday_poll.pass.heartbeat")

    companies = await _iter_active_companies(paperclip_repo)
    if not companies:
        return

    try:
        cookie = await get_admin_session_cookie(admin_client._http)
    except RuntimeError as e:
        logger.warning("friday_poll: admin session unavailable, skipping pass: %s", e)
        put_metric("friday_poll.pass.admin_session_unavailable")
        return

    for company in companies:
        owner_id = company.org_id  # owner = org_id for org members; falls back to user_id below
        company_id = company.company_id
        try:
            issues = await admin_client.list_issues(
                session_cookie=cookie,
                company_id=company_id,
                params={"status": "open"},
            )
        except (PaperclipApiError, httpx.HTTPError) as e:
            logger.warning(
                "friday_poll: list_issues failed for owner=%s company=%s: %s",
                owner_id, company_id, e,
            )
            put_metric("friday_poll.list_issues.error")
            continue

        if not isinstance(issues, list):
            continue

        # NOTE: filtering to "issues created by Friday" requires either
        # a Paperclip ``creator_agent_id`` filter (not currently exposed)
        # or an Isol8-side mapping table populated when Friday calls
        # paperclip_create_issue. v1 reads all open issues and counts;
        # the targeted filter is a follow-up.
        cache = _STATUS_CACHE.setdefault(owner_id, {})
        changed: list[tuple[str, str, str]] = []
        for issue in issues:
            issue_id = issue.get("id")
            status = issue.get("status", "unknown")
            if not isinstance(issue_id, str):
                continue
            prev = cache.get(issue_id)
            if prev is not None and prev != status:
                changed.append((issue_id, prev, status))
            cache[issue_id] = status

        if changed:
            put_metric("friday_poll.status_change_detected", value=len(changed))
            for issue_id, prev, new in changed:
                logger.info(
                    "friday_poll: status change detected owner=%s issue=%s %s→%s",
                    owner_id, issue_id, prev, new,
                )
            # TODO(orchestration): wake Friday for this owner with a
            # synthetic session key (``paperclip:poll:<owner_id>``) so
            # she can call paperclip_get_issue_status and triage. The
            # wake mechanism needs the gateway protocol confirmed —
            # specifically how to send a ``req agent`` frame outside
            # of an issue-driven Paperclip wake. Tracked separately.


async def _iter_active_companies(repo: PaperclipRepo) -> list[Any]:
    """Return all Paperclip companies with status='active'.

    v1 uses a full table scan with a status filter — the table is small
    (one row per Isol8 user, not per company) at current scale. If this
    becomes a hot path, add a ``by-status`` GSI and convert to a Query.

    NOTE: this is not on PaperclipRepo yet — exposing it from the repo
    is the right home but would broaden this PR. The helper here calls
    into the underlying boto3 table directly via the repo's protected
    ``_table()``; that internal access is mirrored elsewhere in the
    backend (see how update_service reads private repo internals for
    its retry pass). When/if a ``list_active`` helper lands on the
    repo, switch this to it.
    """
    out: list[Any] = []
    table = repo._table()
    last_evaluated_key: dict | None = None
    while True:
        # Defer to a thread for the boto3 sync call — same pattern as
        # paperclip_repo.scan_purge_due.
        from core.dynamodb import run_in_thread

        kwargs: dict = {"FilterExpression": "#s = :active", "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":active": "active"}}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            company = await repo.get(item["user_id"])
            if company is not None and company.status == "active":
                out.append(company)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            return out
