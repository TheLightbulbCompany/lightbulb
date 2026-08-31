"""Owner-day rollup composer — the six counters P1's Home tab renders.

Pulls provider truth (Stripe Connect money, PostHog traffic, the usage
counters) for one owner+day and overwrites the ``day:`` row via
``rollup_repo.put_day``. Recompute-and-replace, never increment: the row is a
cache of provider-native numbers, each explainable by a provider link.

Callers: the hourly ``_rollup_pass`` in ``update_service`` (today+yesterday
per connected-Stripe owner) and the Connect money-webhook branches (today).
"""

import logging
from datetime import datetime, timezone

from core.repositories import rollup_repo, usage_repo
from core.services import company_stripe_reads, owner_posthog

logger = logging.getLogger(__name__)


async def refresh_owner_day(owner_id: str, day: str) -> dict | None:
    """Recompute and store the rollup row for owner+day (``YYYY-MM-DD`` UTC).

    Returns the stored values, or ``None`` — nothing written — when every
    source came back empty (no bound Stripe account, no PostHog token, no
    usage row). A miss is a stale tile, never an error.

    A single ``None`` source CARRIES FORWARD the existing row's values for
    that source's keys (stale beats zero on a dashboard); zeros only when no
    prior row exists. The prior row is read once, and only on the outage
    path — the happy path performs no extra read.
    """
    money = await company_stripe_reads.day_money(owner_id, day)
    traffic = await owner_posthog.day_traffic(owner_id, day)

    usage = None
    if day[:7] == datetime.now(timezone.utc).strftime("%Y-%m"):
        # ponytail: agent spend is the month-to-date total stamped on
        # current-month rows only — a true per-day inference split needs
        # per-day usage rows, not worth it until Home ships.
        usage = await usage_repo.get_period_usage(owner_id, day[:7])

    if money is None and traffic is None and usage is None:
        return None

    prior: dict = {}
    if money is None or traffic is None or usage is None:
        # Carry forward on outage. This is also what keeps a prior-month
        # day's final agent-spend stamp intact: usage is skipped for
        # past-month days, so the month-boundary pass lands here and reuses
        # the stamped value instead of zeroing it.
        rows = await rollup_repo.get_days(owner_id, day, day)
        prior = rows[0] if rows else {}

    values = {
        "revenue_microcents": (money or prior).get("revenue_microcents", 0),
        "signups": (money or prior).get("signups", 0),
        "visitors": (traffic or prior).get("visitors", 0),
        # bundle.social is P3 — honest zero, no source to carry forward; the
        # tile hides when every value in range is 0.
        "followers": 0,
        # No owner-scoped open-thread store exists yet: agent_mail_inbox_repo
        # is address-keyed messages with no thread/open state. P2's Support
        # task must wire a thread store (open/closed status + owner index)
        # and replace this honest zero.
        "open_threads": 0,
        # total_spend_microdollars is actually microcents (misnomer noted in
        # usage_service.py:60) — no conversion needed.
        "agent_spend_microcents": usage["total_spend_microdollars"]
        if usage is not None
        else prior.get("agent_spend_microcents", 0),
    }
    await rollup_repo.put_day(owner_id, day, values)
    return values
