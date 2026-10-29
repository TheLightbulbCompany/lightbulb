"""Managed per-agent inference cost -> Paperclip cost events.

The openrouter-settlement Lambda reports each settled managed call here (it cannot talk to
Paperclip: admin auth is a session cookie). Paperclip stores integer CENTS and a managed call
costs a fraction of one, so the sub-cent remainder is carried per (agent, month) on the
PaperclipCompany row (``agent_cost_remainder``) and whole cents are posted as they accrue.
The carry is internal bookkeeping, not a budget: Paperclip owns the budget and its enforcement.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie, invalidate_admin_session
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner

logger = logging.getLogger(__name__)

MICROCENTS_PER_CENT = 10_000
# ponytail: keep ~3 months of buckets; a sub-cent tail stranded in a rolled-over month is never posted.
_KEEP_MONTHS = 3


def _month_index(ym: str) -> int:
    return int(ym[:4]) * 12 + int(ym[5:7])


async def _post(company_id: str, agent_id: str, model: str, cents: int, occurred_at: str) -> None:
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    kw = dict(company_id=company_id, agent_id=agent_id, model=model, cost_cents=cents, occurred_at=occurred_at)
    try:
        await admin.post_cost_event(session_cookie=cookie, **kw)
    except PaperclipApiError as e:
        if e.status_code != 401:
            raise
        invalidate_admin_session()
        await admin.post_cost_event(session_cookie=await get_admin_session_cookie(admin._http), **kw)


async def record_cost(
    *,
    owner_id: str,
    agent_id: str,
    cost_microcents: int,
    model: str,
    occurred_at: str,
    repo: PaperclipRepo | None = None,
) -> int:
    """Accrue ``cost_microcents`` for the agent's month; post whole cents once >= 1. Returns cents posted."""
    if cost_microcents <= 0:
        return 0
    repo = repo or PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        return 0
    ym = datetime.fromisoformat(occurred_at).astimezone(timezone.utc).strftime("%Y-%m")
    key = f"{agent_id}#{ym}"
    bucket = await repo.add_agent_cost_remainder(owner_id, key, cost_microcents)
    if bucket is None:
        return 0
    stale = [k for k in bucket if _month_index(k.rsplit("#", 1)[1]) <= _month_index(ym) - _KEEP_MONTHS]
    await repo.prune_agent_cost_remainder(owner_id, stale)
    cents = int(bucket[key]) // MICROCENTS_PER_CENT
    if cents < 1:
        return 0
    # Claim first (conditional subtract), post second: two racing settlements cannot both post the
    # same cents. A failed post refunds the claim so the next call retries.
    if (
        await repo.add_agent_cost_remainder(
            owner_id, key, -cents * MICROCENTS_PER_CENT, need=cents * MICROCENTS_PER_CENT
        )
        is None
    ):
        return 0
    try:
        await _post(company_id, agent_id, model, cents, occurred_at)
    except Exception:
        await repo.add_agent_cost_remainder(owner_id, key, cents * MICROCENTS_PER_CENT)
        raise
    return cents
