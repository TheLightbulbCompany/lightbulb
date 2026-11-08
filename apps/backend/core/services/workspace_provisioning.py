"""Workspace provisioning on our own stores: the workspace META row and the starter roster.

* ``provision_org`` writes the META row (``active``, the provisioner as first member, a fresh
  onboarding generation) and seeds ``STARTER_ROSTER`` into ``agent_registry``.
* ``provision_member`` records a member on an active workspace's row.
* ``disable_workspace`` marks a deleted Clerk org's row ``disabled`` with a purge timer; the
  cleanup cron (``update_service._workspace_purge_pass``) deletes it afterwards.

Leaving a workspace is ``PaperclipRepo.remove_member`` (the class keeps its name until task 9).
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError
from ulid import ULID

from core.repositories.paperclip_repo import PaperclipRepo, PaperclipWorkspace
from core.services import agent_registry

logger = logging.getLogger(__name__)

STARTER_ROSTER = (
    ("ceo", "CEO", "ceo", "CEO", None),
    ("engineering", "Engineering", "engineer", "Engineering", "ceo"),
    ("finance", "Finance", "cfo", "Finance", "ceo"),
    ("growth", "Growth", "cmo", "Growth", "ceo"),
    ("analytics", "Analytics", "researcher", "Analytics", "ceo"),
    ("customer_operations", "Customer Operations", "general", "Customer Operations", "ceo"),
)

#: The starter roles, in seed order. ``messaging``/``user_provision`` check a company's
#: ``starter_agent_ids`` against exactly this set.
STARTER_ROLE_KEYS = tuple(row[0] for row in STARTER_ROSTER)

#: Each starter's persona (the registry ``persona``). ``hermes_profiles.build_soul_md``
#: renders it into the agent's SOUL.md under "## What you do" -- without it that file is one line.
#: Keyed by role key (not a sixth tuple element) so every ``STARTER_ROSTER`` unpacking stays as is.
STARTER_CAPABILITIES = {
    "ceo": (
        "Sets direction, splits work across the team, reports back."
        "\n\n"
        "Owns the company's goals and the plan. Answers the owner and does quick work itself; delegates "
        "specialist work to the right teammate with what done means, and follows it to done. Keeps "
        "BRIEF.md and the goals current, checks on stalled tasks, and posts a short progress note when "
        "work lands. Tools when connected: Google Workspace or Microsoft 365 (calendar, mail, docs), "
        "Notion, Linear."
    ),
    "engineering": (
        "Builds and fixes product and site; owns code, deploys and technical debt."
        "\n\n"
        "Owns anything that ships as code: the product, the website, integrations, scripts. First step "
        "on any request: find the repo or site, read what exists, and propose the smallest change before"
        " building. Works in branches and PRs, never pushes untested changes, and reports what shipped "
        "and how to verify it. Tools when connected: GitHub, Vercel, Sentry, Cloudflare, AWS. Hands "
        "revenue/pricing questions to Finance, instrumentation to Analytics. For a project built from "
        "scratch, follows the lightbulb skill's references/software-projects.md."
    ),
    "finance": (
        "Tracks money in and out; owns revenue, costs, pricing and runway."
        "\n\n"
        "Owns the numbers that cost or make money. First step: pull the current picture (revenue, MRR, "
        "refunds, largest costs) before giving an opinion. Answers with figures and their source, flags "
        "anything unusual (failed payments, churn spikes, surprise spend), and drafts pricing or budget "
        "changes for the owner to approve, never executing a payment itself. Tools when connected: "
        "Stripe, QuickBooks. Hands growth spend questions to Growth, metric definitions to Analytics."
    ),
    "growth": (
        "Finds and wins customers; owns marketing, content, outreach and campaigns."
        "\n\n"
        "Owns getting attention and turning it into customers. When asked for a piece (a post, an email, "
        "copy), delivers that piece alone, ready to use; asks who the customer is when it matters, and never "
        "invents targets or numbers. Writes posts, landing copy, emails and outreach lists; runs campaigns only "
        "with the owner's approval. Tools when connected: X, Instagram, Meta Ads, Apollo, Attio, Google "
        "Workspace. Hands results to Analytics to measure, site changes to Engineering."
    ),
    "analytics": (
        "Measures what's happening; owns metrics, dashboards, research and competitor analysis."
        "\n\n"
        'Owns answering "is it working?" with evidence. First step: state the question as a metric and '
        "name the data source, then report the number, the trend, and one sentence on what it means. "
        "Also does market and competitor research with sources cited. Tools when connected: PostHog, "
        "Stripe (read), Parallel web research, Google Sheets. Hands missing tracking to Engineering, "
        "findings that change the plan to the CEO."
    ),
    "customer_operations": (
        "Looks after customers; owns support, onboarding, feedback and retention."
        "\n\n"
        "Owns every customer conversation after the sale. First step on a ticket: read the customer's "
        "history, answer or fix what can be done now, and escalate what can't with the context attached."
        " Turns repeated questions into help docs, collects feedback into themes for the CEO, and flags "
        "churn risks early. Tools when connected: Intercom, Google Workspace or Microsoft 365 mail, "
        "Attio, Linear (bug reports). Hands bugs to Engineering, refunds to Finance."
    ),
}


#: Starter ids are uuid5(org, role key): two concurrent provision runs (the executor has no lease)
#: replay the same registry rows instead of seeding a second roster.
_STARTER_NAMESPACE = uuid.UUID("3b1f0c52-8a8e-4f8e-9d0f-6c2f4d7e9a11")


def _repo() -> PaperclipRepo:
    return PaperclipRepo(table_name="paperclip-companies")


def starter_agent_id(org_id: str, starter_key: str) -> str:
    return str(uuid.uuid5(_STARTER_NAMESPACE, f"{org_id}:{starter_key}"))


async def provision_org(owner_id: str, *, provisioner: str) -> PaperclipWorkspace:
    """Make ``owner_id``'s workspace active with the full starter roster. Idempotent.

    An active row with the full starter set returns as is. Otherwise each missing starter is created in the registry
    (deterministic id, so a retry or a concurrent run adopts the same row) and the META row is
    written ``active`` with ``provisioner`` as a member. A non-active row's other fields
    (``company_id`` on old rows, onboarding, name) are carried forward. Any failure marks the
    row ``failed`` (never over an ``active`` one) and re-raises.
    """
    repo = _repo()
    ws = await repo.get_workspace(owner_id, consistent_read=True)
    starters = dict((ws.starter_agent_ids if ws is not None else None) or {})
    if ws is not None and ws.status == "active" and all(starters.get(k) for k in STARTER_ROLE_KEYS):
        return ws
    try:
        for starter_key, name, role, title, parent_key in STARTER_ROSTER:
            if not starters.get(starter_key):
                spec = {
                    "name": name,
                    "role": role,
                    "title": title,
                    "persona": STARTER_CAPABILITIES[starter_key],
                    "reports_to": starters[parent_key] if parent_key else None,
                }
                agent = await agent_registry.create(owner_id, agent_id=starter_agent_id(owner_id, starter_key), **spec)
                if agent.get("status") == "deleted":  # the owner deleted this starter: its id stays reserved
                    agent = await agent_registry.create(owner_id, **spec)
                starters[starter_key] = agent["id"]
        now = datetime.now(timezone.utc)
        fields = {
            "status": "active",
            "updated_at": now,
            "provisioner_user_id": (ws.provisioner_user_id if ws is not None else "") or provisioner,
            "member_user_ids": (set(ws.member_user_ids) if ws is not None else set()) | {provisioner},
            "starter_agent_ids": starters,
            "last_error": None,
        }
        if ws is not None:
            row = dataclasses.replace(ws, **fields)
        else:
            row = PaperclipWorkspace(org_id=owner_id, company_id="", created_at=now, **fields)
        if not row.onboarding_generation:
            row.onboarding_generation = str(ULID())
            row.funnel_state = "not_started"
        # A concurrent run that landed first keeps its generation: adopt it and put once more.
        try:
            await repo.put_workspace(
                row,
                condition="attribute_not_exists(onboarding_generation) OR onboarding_generation = :g",
                condition_values={":g": row.onboarding_generation},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            winner = await repo.get_workspace(owner_id, consistent_read=True)
            if winner is not None and winner.onboarding_generation:
                row.onboarding_generation = winner.onboarding_generation
                row.funnel_state = winner.funnel_state
            await repo.put_workspace(row)
    except Exception as e:
        await _mark_failed(repo, owner_id, f"provision_org failed: {e}"[:1000])
        raise
    logger.info("workspace_provisioning.provision_org: %s active (provisioner=%s)", owner_id, provisioner)
    return row


async def provision_member(owner_id: str, user_id: str) -> PaperclipWorkspace | None:
    """Record ``user_id`` as a member of the active workspace. None while it is not active yet."""
    repo = _repo()
    ws = await repo.resolve_owner_row(owner_id)
    if ws is not None and user_id not in ws.member_user_ids:
        await repo.add_member(owner_id, user_id)
        ws.member_user_ids.add(user_id)
    return ws


async def disable_workspace(org_id: str, *, grace_days: int = 30) -> None:
    """Mark a workspace row ``disabled`` with a purge timer (the Clerk org was deleted)."""
    repo = _repo()
    if await repo.get_workspace(org_id) is None:
        return
    purge_at = datetime.now(timezone.utc) + timedelta(days=grace_days)
    await repo.update_status(org_id, status="disabled", scheduled_purge_at=purge_at)
    logger.info("workspace_provisioning.disable_workspace: %s scheduled for purge at %s", org_id, purge_at.isoformat())


async def _mark_failed(repo: PaperclipRepo, org_id: str, reason: str) -> None:
    """Leave the failure visible (``/teams`` answers 503, not an endless 202), never over ``active``."""
    if await repo.get_workspace(org_id, consistent_read=True) is not None:
        await repo.update_status(org_id, status="failed", last_error=reason, not_if_status="active")
    else:
        await repo.put_failed_marker_if_absent(org_id=org_id, reason=reason)
