"""Backend orchestration tools that Friday calls to delegate work.

Three functions correspond to the three OpenClaw tools Friday will be
granted:

  - ``paperclip_list_agents``       → roster discovery
  - ``paperclip_create_issue``      → primary delegation action
  - ``paperclip_get_issue_status``  → triage on issue-update wakes

All three are owner-scoped: callers pass ``owner_id`` explicitly. The
HTTP boundary that exposes these to Friday's container is intentionally
NOT in this module — that's the next PR. The functions here are the
service layer; wiring the OpenClaw tool-call protocol to them is a
separate concern.

Mirrors ``fetch_paperclip_agents_for_owner`` in
``paperclip_openclaw_pairing.py`` for company resolution + admin
session-cookie acquisition. Anywhere the existing pairing helper falls
back to ``[]`` (no Paperclip company yet, transient Paperclip outage),
these tools raise ``FridayToolError`` instead — Friday is expected to
report the failure to the user rather than silently degrade.

See the design spec at
``docs/superpowers/specs/2026-05-13-friday-orchestration-design.md``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import _resolve_company_id_for_owner

logger = logging.getLogger(__name__)


class FridayToolError(Exception):
    """Domain error from a Friday orchestration tool.

    Carries a stable ``code`` field that Friday's prompt can pattern-match
    on (so the agent can decide whether to retry, ask the user, or hand
    off to Atlas) plus a human-readable ``message`` for surfacing.

    Codes:
      - ``owner_has_no_company`` — Paperclip company doesn't exist yet
        (provisioning hasn't run or failed). Friday should not retry.
      - ``assignee_not_in_roster`` — assignee UUID is not a current
        Paperclip agent for this owner. Friday's roster lookup should
        prevent this in normal flow; defense-in-depth check.
      - ``paperclip_unavailable`` — transient Paperclip API / transport
        error. Friday may retry once before reporting.
      - ``admin_credentials_unavailable`` — admin session cookie
        couldn't be acquired. Operator-side problem; Friday should
        report and stop.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def _sort_roster(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """CEO first, then alphabetical by name.

    Friday sees herself at the top when she reads her own roster —
    matches the mental model that the CEO is the entry point.
    """

    def _key(a: dict[str, Any]) -> tuple[int, str]:
        ceo_rank = 0 if a.get("role") == "ceo" else 1
        return (ceo_rank, a.get("name", ""))

    return sorted(agents, key=_key)


async def _get_company_id_or_raise(
    *,
    owner_id: str,
    repo: PaperclipRepo,
) -> str:
    """Resolve company_id or raise FridayToolError('owner_has_no_company')."""
    company_id = await _resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if company_id is None:
        raise FridayToolError(
            "owner_has_no_company",
            f"owner_id={owner_id!r} has no Paperclip company — provisioning hasn't completed",
        )
    return company_id


async def _get_cookie_or_raise(admin_client: PaperclipAdminClient) -> str:
    """Acquire the admin session cookie or raise FridayToolError.

    ``get_admin_session_cookie`` raises RuntimeError on Secrets Manager
    misses / malformed payloads / missing Set-Cookie — translate that
    to a structured tool error Friday can interpret.
    """
    try:
        return await get_admin_session_cookie(admin_client._http)
    except RuntimeError as e:
        raise FridayToolError(
            "admin_credentials_unavailable",
            f"paperclip admin session unavailable: {e}",
        ) from e


async def paperclip_list_agents(
    *,
    owner_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> list[dict[str, Any]]:
    """Return the current Paperclip agent roster for ``owner_id``.

    CEO-first sorted. Empty company → ``owner_has_no_company`` error.
    Paperclip outage → ``paperclip_unavailable`` error. Both are
    structured so Friday's prompt can decide how to react.
    """
    company_id = await _get_company_id_or_raise(owner_id=owner_id, repo=repo)
    cookie = await _get_cookie_or_raise(admin_client)

    try:
        agents = await admin_client.list_agents(
            session_cookie=cookie,
            company_id=company_id,
        )
    except (PaperclipApiError, httpx.HTTPError) as e:
        put_metric("friday_tools.list_agents.error")
        logger.warning(
            "friday_tools.list_agents: paperclip unavailable for owner=%s company=%s: %s",
            owner_id, company_id, e,
        )
        raise FridayToolError(
            "paperclip_unavailable",
            f"paperclip list_agents failed: {e}",
        ) from e

    if not isinstance(agents, list):
        logger.warning(
            "friday_tools.list_agents: non-list response for owner=%s — got %s",
            owner_id, type(agents).__name__,
        )
        raise FridayToolError(
            "paperclip_unavailable",
            f"paperclip list_agents returned non-list ({type(agents).__name__})",
        )

    sorted_agents = _sort_roster(agents)
    put_metric("friday_tools.list_agents.success", value=len(sorted_agents))
    return sorted_agents


async def paperclip_create_issue(
    *,
    owner_id: str,
    title: str,
    description: str,
    assignee_agent_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
    priority: str | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Create a Paperclip issue and assign it to ``assignee_agent_id``.

    Validates that ``assignee_agent_id`` is in the owner's current roster
    before forwarding to Paperclip — defense in depth, so a stale agent
    id in Friday's context can't accidentally route work to a different
    tenant's agent if Paperclip ever loosens its own assignee checks.

    Returns the Paperclip issue dict (``{id, ...}``) on success.
    """
    company_id = await _get_company_id_or_raise(owner_id=owner_id, repo=repo)
    cookie = await _get_cookie_or_raise(admin_client)

    try:
        roster = await admin_client.list_agents(
            session_cookie=cookie,
            company_id=company_id,
        )
    except (PaperclipApiError, httpx.HTTPError) as e:
        put_metric("friday_tools.create_issue.error")
        raise FridayToolError(
            "paperclip_unavailable",
            f"roster lookup failed before create: {e}",
        ) from e

    if not isinstance(roster, list) or not any(
        a.get("id") == assignee_agent_id for a in roster
    ):
        put_metric("friday_tools.create_issue.rejected_assignee")
        raise FridayToolError(
            "assignee_not_in_roster",
            f"assignee_agent_id={assignee_agent_id!r} is not in owner's roster",
        )

    # Paperclip expects camelCase body keys.
    body: dict[str, Any] = {
        "title": title,
        "description": description,
        "assigneeAgentId": assignee_agent_id,
    }
    if priority is not None:
        body["priority"] = priority
    if project_id is not None:
        body["projectId"] = project_id

    try:
        result = await admin_client.create_issue(
            session_cookie=cookie,
            company_id=company_id,
            body=body,
        )
    except (PaperclipApiError, httpx.HTTPError) as e:
        put_metric("friday_tools.create_issue.error")
        logger.warning(
            "friday_tools.create_issue: paperclip create_issue failed for owner=%s assignee=%s: %s",
            owner_id, assignee_agent_id, e,
        )
        raise FridayToolError(
            "paperclip_unavailable",
            f"paperclip create_issue failed: {e}",
        ) from e

    put_metric("friday_tools.create_issue.success")
    logger.info(
        "friday_tools.create_issue: owner=%s issue=%s assignee=%s",
        owner_id, result.get("id"), assignee_agent_id,
    )
    return result


async def paperclip_get_issue_status(
    *,
    owner_id: str,
    issue_id: str,
    repo: PaperclipRepo,
    admin_client: PaperclipAdminClient,
) -> dict[str, Any]:
    """Read an issue's current status + last thread message.

    Friday calls this on issue-update wakes (PR 3) and proactively when
    checking on outstanding work. Returns the raw Paperclip issue dict;
    Friday's prompt is responsible for interpreting it.
    """
    # company_id resolution still runs as a tenancy check even though
    # get_issue is keyed only on issue_id — if the owner has no company,
    # they shouldn't be reading issues.
    await _get_company_id_or_raise(owner_id=owner_id, repo=repo)
    cookie = await _get_cookie_or_raise(admin_client)

    try:
        issue = await admin_client.get_issue(
            session_cookie=cookie,
            issue_id=issue_id,
        )
    except (PaperclipApiError, httpx.HTTPError) as e:
        put_metric("friday_tools.get_issue_status.error")
        logger.warning(
            "friday_tools.get_issue_status: failed for owner=%s issue=%s: %s",
            owner_id, issue_id, e,
        )
        raise FridayToolError(
            "paperclip_unavailable",
            f"paperclip get_issue failed: {e}",
        ) from e

    put_metric("friday_tools.get_issue_status.success")
    return issue
