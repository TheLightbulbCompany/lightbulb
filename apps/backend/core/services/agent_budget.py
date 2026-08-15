"""The per-owner agent budget — the plan limit, and the one guard every
agent-creation path calls.

**Why a shared guard rather than a check per router.** Agents arrive from four
independent paths, and a limit enforced on some of them is not a limit:

1. ``routers/websocket_chat._intercept_agents_create`` — the /chat ``agents.create`` RPC.
2. ``routers/teams/agents.create_agent`` — the Teams Inbox REST endpoint.
3. ``routers/marketplace_listings.deploy_listing`` — a single-agent hire. Gated
   at the router, not in ``catalog_service.deploy_from_artifact``, because the
   Atlas bridge (4) also calls that service and would double-check.
4. ``routers/agent_builder.register_built_agent`` — the Atlas bridge.

(4) is the one that makes this non-obvious. Atlas is a seeded agent that runs
INSIDE the owner's container and recruits new agents on its own initiative, so
it never crosses the browser→backend WS path the /chat interception sits on.

**What makes that catchable is credential scoping, not Atlas's prompt.** Atlas's
seeded AGENTS.md does say "the CREATE is the one thing Atlas can't do
gateway-free", but instructions to a model are not an enforcement boundary. The
actual control is that the container holds NO Paperclip credential —
``ecs_manager`` strips every ``PAPERCLIP_*`` var from per-user task definitions
and the admin session cookie is backend-side only — so minting a Paperclip agent
genuinely requires coming through this backend. Keep it that way: handing the
container a Paperclip credential would turn this guard into a suggestion.

**Known blind spot.** Nothing stops the container calling its OWN gateway's
``agents.create`` to make a container-local OpenClaw agent. That agent never
reaches Paperclip, so it is not counted here and not refused. It does not
survive in the roster — a config regen rebuilds ``agents.list`` from Paperclip
and ``_deep_merge`` replaces non-dict values — but its workspace dir, SQLite
database and Litestream replica persist, which is exactly the orphan-replica
class that inflated one owner's boot from 11 live agents to 23 restored
databases. Counting the roster bounds the PRODUCT; it does not bound boot cost.

**The budget counts the LIVE ROSTER, which includes the seeded Friday + Atlas.**
That matches what ``stripe_webhook_handlers._apply_ultra_resize`` already
compares against the same constant, so an owner does not get a different answer
depending on which code asked. Practically it means a standard-plan owner can
run 8 of their own agents alongside the two seeds.

**This guard FAILS OPEN.** An undeterminable count (Paperclip unreachable →
``count == 0``) allows the create. Blocking every agent creation across the
fleet because Paperclip is briefly down would be a worse failure than briefly
letting an owner past the cap — and it is not the only defence: the
count-derived size floor in ``ecs_manager._size_for_owner`` still puts an
over-budget owner on the bigger box on their next roll. Product limit here,
reliability backstop there; the two are deliberately independent.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from core.observability.metrics import put_metric
from core.services.company_package import COMPANY_STANDARD_PLAN_MAX_AGENTS

logger = logging.getLogger(__name__)

AGENT_BUDGET_EXCEEDED_DETAIL = (
    f"This workspace is at its {COMPANY_STANDARD_PLAN_MAX_AGENTS}-agent limit. Upgrade to Ultra Pro to add more agents."
)


async def is_at_agent_budget(owner_id: str) -> bool:
    """True when ``owner_id`` may NOT create another agent.

    The decision half, split out from ``enforce_agent_budget`` because not every
    caller can answer with an HTTP status. ``marketplace_hire.create_hire_approval``
    runs inside the ``/recommend`` response and its established contract is to
    return None and stay "degraded-but-useful" rather than fail the whole call,
    so it needs the boolean.

    False (allow) on every unreadable input — see the fail-open note above.
    """
    from core.repositories import billing_repo
    from core.services.paperclip_openclaw_pairing import count_paperclip_agents_for_owner

    try:
        account = await billing_repo.get_by_owner_id(owner_id)
    except Exception:  # noqa: BLE001 — a billing outage must not block creation.
        logger.warning("agent-budget: billing read failed for %s; allowing", owner_id, exc_info=True)
        return False
    if (account or {}).get("plan") == "ultra_pro":
        return False

    try:
        agent_count = await count_paperclip_agents_for_owner(owner_id)
    except Exception:  # noqa: BLE001 — see the fail-open note in the module docstring.
        logger.warning("agent-budget: count failed for %s; allowing", owner_id, exc_info=True)
        return False

    if agent_count == 0:
        # UNDETERMINABLE, not empty: every provisioned owner has Friday + Atlas,
        # so a reachable roster is always >= 2. Allow rather than block the
        # whole fleet's agent creation on a Paperclip blip.
        put_metric("agent_budget.count_unavailable")
        return False

    if agent_count >= COMPANY_STANDARD_PLAN_MAX_AGENTS:
        put_metric("agent_budget.blocked", dimensions={"plan": str((account or {}).get("plan", "unknown"))})
        logger.info(
            "agent-budget: refusing create for owner %s — %d agents at the %d limit",
            owner_id,
            agent_count,
            COMPANY_STANDARD_PLAN_MAX_AGENTS,
        )
        return True
    return False


async def enforce_agent_budget(owner_id: str) -> None:
    """Raise 409 if creating one more agent would put ``owner_id`` over budget.

    Returns None (allow) when the owner is on a plan without the cap, when they
    are still under it, or when the live count cannot be determined. Never
    raises anything but ``HTTPException``.

    409 (not 402) matches the existing over-budget refusal in
    ``routers/marketplace_listings`` — "Requires Ultra Pro" — so clients see one
    shape for "this needs a bigger plan" rather than two.
    """
    if await is_at_agent_budget(owner_id):
        raise HTTPException(status_code=409, detail=AGENT_BUDGET_EXCEEDED_DETAIL)
