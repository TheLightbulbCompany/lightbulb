"""Escalate a Paperclip issue that recovery blocked after a platform fault.

When an agent's run on an issue fails with a platform code, the fork's recovery
(``server/src/services/recovery/service.ts``) retries it (``setup_failed`` and
``process_lost`` once, ``timeout`` up to 3x with backoff), then
``escalateStrandedAssignedIssue`` sets ``status: "blocked"`` and hands the issue to a
"recovery owner" (the failed agent itself for ``process_lost``, else its manager). Those
failures say nothing about the task, and a blocked issue sits there: the owner ruling is
"the work must get done". So every forwarded failure schedules ONE delayed look at the
issue, and if recovery blocked it we hand it to a live agent instead.

The ladder is stateless, keyed on which agent just failed: failed agent -> escalation
agent (per-owner override, else the starter Engineering agent, else the root CEO) -> root
CEO -> give up (``paperclip.escalation.exhausted``, pages P33). A rung whose agent cannot
be invoked (paused / terminated / pending approval) is skipped.

One PATCH ``{assigneeAgentId, status: "todo"}`` does the handoff: blocked -> todo cancels
the active ``issue_recovery_actions`` row (fork ``routes/issues.ts``
``blockedToTodoRecovery``) and wakes the assignee. Moving the assignee while leaving it
blocked would get the new run cancelled as blocked (Paperclip audit 2026-09-24 F6). Real
open blockers make that PATCH 422, and then the issue is left as it is.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import _is_root_ceo, _rows

logger = logging.getLogger(__name__)

ESCALATION_ERROR_CODES = frozenset({"setup_failed", "process_lost", "timeout"})
# Recovery escalates on the scheduler tick after the last retry fails (fork default
# HEARTBEAT_SCHEDULER_INTERVAL_MS = 30 s), so 90 s leaves it three ticks. A check that
# lands before the block is a no-op; the next failure's check catches it.
CHECK_DELAY_S = 90
# Fork packages/shared/src/agent-eligibility.ts INVOKABLE_AGENT_STATUSES.
_INVOKABLE_STATUSES = frozenset({"active", "idle", "running", "error"})

_tasks: set[asyncio.Task] = set()


def schedule(event: dict) -> None:
    """Fire-and-forget one delayed check for a platform-failed issue run."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    code = payload.get("errorCode")
    if (
        event.get("eventType") != "agent.run.failed"
        or code not in ESCALATION_ERROR_CODES
        or not payload.get("issueId")
        or not payload.get("agentId")
    ):
        return
    # ponytail: in-process timer, lost if this backend task stops inside the delay; the
    # issue then just stays blocked. A durable delay (SQS/SFN) if that ever matters.
    task = asyncio.create_task(_check_later(str(event.get("companyId")), payload["issueId"], payload["agentId"], code))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _check_later(company_id: str, issue_id: str, failed_agent_id: str, error_code: str) -> None:
    await asyncio.sleep(CHECK_DELAY_S)
    try:
        await escalate(company_id, issue_id, failed_agent_id, error_code)
    except Exception:
        logger.exception("paperclip escalation failed company=%s issue=%s", company_id, issue_id)


async def _resolve_owner(company_id: str) -> tuple[str | None, dict]:
    """The owner id for a Paperclip company plus its starter-agent map."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    for user_id in await repo.get_user_ids_for_company(company_id):
        row = await repo.get(user_id)
        if row is None:
            continue
        owner_id = row.org_id or row.user_id
        owner_row = await repo.resolve_owner_row(owner_id)
        return owner_id, (owner_row.starter_agent_ids if owner_row else None) or {}
    return None, {}


async def escalate(company_id: str, issue_id: str, failed_agent_id: str, error_code: str) -> None:
    owner_id, starter_ids = await _resolve_owner(company_id)
    if owner_id is None:
        logger.warning("paperclip escalation: no owner for company=%s issue=%s", company_id, issue_id)
        return

    async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        issue = await admin.get_issue(session_cookie=cookie, issue_id=issue_id) or {}
        # Idempotent: once handed off the issue is no longer blocked, so a duplicate event
        # (or a second backend task) finds nothing to do. Residual race: two checks reading
        # "blocked" at the same instant both PATCH the same target and both comment.
        if issue.get("status") != "blocked":
            return
        if issue.get("assigneeUserId") or not issue.get("assigneeAgentId"):
            return  # human-assigned issues are left alone

        agents = {
            a["id"]: a
            for a in _rows(await admin.list_agents(session_cookie=cookie, company_id=company_id), "agents")
            if a.get("id")
        }
        ceo = next((a["id"] for a in agents.values() if _is_root_ceo(a)), None)
        escalation = (
            settings.PAPERCLIP_ESCALATION_AGENT_OVERRIDES.get(owner_id) or starter_ids.get("engineering") or ceo
        )
        if failed_agent_id == ceo:
            candidates = []
        elif failed_agent_id == escalation:
            candidates = [ceo]
        else:
            candidates = [escalation, ceo]
        target = next(
            (
                agent_id
                for agent_id in candidates
                if agent_id
                and agent_id != failed_agent_id
                and agents.get(agent_id, {}).get("status") in _INVOKABLE_STATUSES
            ),
            None,
        )
        if target is None:
            put_metric("paperclip.escalation.exhausted")
            logger.error(
                "paperclip escalation exhausted: owner=%s issue=%s failed_agent=%s code=%s left blocked",
                owner_id,
                issue_id,
                failed_agent_id,
                error_code,
            )
            return

        # Deliberately NOT skipped when the assignee already equals the target: recovery
        # hands the issue to the failed agent's manager (the CEO for starter specialists),
        # so the CEO rung usually finds it blocked on the CEO already, and only the
        # blocked -> todo move actually gets the work running again.
        try:
            await admin.patch_issue(
                session_cookie=cookie,
                issue_id=issue_id,
                body={"assigneeAgentId": target, "status": "todo"},
            )
        except PaperclipApiError as exc:
            if exc.status_code != 422:
                raise
            put_metric("paperclip.escalation.refused")
            logger.warning("paperclip escalation refused (open blockers?): issue=%s %s", issue_id, exc)
            return
        rung = "ceo" if target == ceo else "escalation"
        put_metric("paperclip.escalation.escalated", dimensions={"rung": rung})
        logger.info("paperclip escalation: issue=%s %s -> %s (%s)", issue_id, failed_agent_id, target, rung)

        failed_name = agents.get(failed_agent_id, {}).get("name") or "the previous agent"
        comment = (
            f"Escalated from {failed_name}: its runs kept failing with {error_code} — a platform fault, "
            f"not the task. Reassigned to {agents[target].get('name') or target}."
        )
        try:
            await admin.add_issue_comment(session_cookie=cookie, issue_id=issue_id, body={"body": comment})
        except Exception:
            logger.exception("paperclip escalation: comment failed issue=%s (reassignment stands)", issue_id)
