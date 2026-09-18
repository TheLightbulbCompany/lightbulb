"""Full teardown of a Clerk-ORG-owned workspace — the org-shaped sibling of
``core/services/account_wipe.py``.

``account_wipe`` is personal-only by design (``_refuse_org_owners`` raises
``OrgWipeUnsupportedError``: "org accounts have per-member fan-out ... a
documented follow-up"). Every owner is an org now, so there was no way to
delete a workspace at all. This is that follow-up, as an ops script rather
than an admin route: it composes the SAME teardown step functions
``account_wipe`` calls — including its ``WipeStep``/``WipeReport``/``_step``
— and adds only what an org needs that a user does not: the per-member loop,
the Clerk organization delete, and the org-specific refusals.

Runs INSIDE the backend task (ECS exec) — it needs the EFS mount for the
ECS-era workspace delete, the VPC for Aurora/RDS Data API, and the task role
for ECS/S3/Secrets Manager. Both invocations work:

    uv run python -m scripts.wipe_org --org org_XXX          # from apps/backend
    uv run python /tmp/wipe_org.py    --org org_XXX          # copied file, cwd /app

    ... --execute                       # commit
    ... --execute --delete-clerk-users  # also delete each member's Clerk user

PREVIEW IS THE DEFAULT. Without ``--execute`` nothing is deleted: every step
that touches anything goes through ``_do`` below, which is the ONE call site
deciding between "print what I would touch" and "touch it", so the preview and
the execution can never disagree about scope. The single exception is
``_clerk_user_step``'s two REFUSED lines, which are report entries for a
delete that is never attempted in either mode (``_do`` has no "refused" case).

Exit codes: 0 all steps ok/skipped, 1 at least one step failed OR the
Paperclip side could not be resolved (see ``_NO_PAPERCLIP_ROW``), 2 a refusal
(checked for EVERY named org before ANY org's first step, so a run either
starts clean or does nothing).

Idempotent: every collaborator here already treats "already gone" as success
(they were written for a retryable wipe), and the inventory read decides only
what to REPORT, never that a delete may be skipped.

WHAT IT DOES NOT TOUCH: the ``admin-actions`` table — it is the audit trail,
and this script APPENDS one row per executed org rather than deleting any.

SELF-CONTAINED ON PURPOSE. This file is COPIED into a running backend task, so
it executes against whatever ``core/`` that task's image was built with — NOT
this checkout. Four things it needs are missing or WRONG in the deployed
modules, and all four are implemented *here*, against those modules' current
signatures, rather than by changing them:

* ``_clerk_delete_organization``   — clerk_admin has no org delete.
* ``_clerk_org_with_member_count`` — ``clerk_admin.get_organization`` takes no
  ``include_members_count``, and the count is what proves Clerk answered.
* ``_company_row_user_ids``        — ``get_user_ids_for_company`` filters to
  ACTIVE rows; the shared-company guard must see disabled ones too.
* ``_revoke_sessions``             — ``clerk_admin.revoke_sessions`` posts to
  ``/v1/users/{id}/revoke_session``, which is not a Clerk Backend API route.

Each is built from primitives the deployed modules already expose
(``clerk_admin._CLERK_API_BASE``/``_TIMEOUT_S``, ``PaperclipRepo._query_pages``),
so there is exactly one definition of the base URL and one pagination loop. Do
not "clean this up" by moving them into core/: the copy would then TypeError on
a task whose image predates that change, which is every task until the next
promotion.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable

# Make ``core.*`` importable however this file is invoked. Two shapes matter:
# ``python -m scripts.wipe_org`` from apps/backend (parent of this file's dir
# holds ``core/``), and a COPY of this single file dropped somewhere else and
# run from the deployed backend's /app (``uv run python /tmp/wipe_org.py``),
# where the file's own location says nothing — the cwd is the backend root.
# Same shim as scripts/bootstrap_paperclip_admin.py, plus the cwd fallback.
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if not os.path.isdir(os.path.join(_BACKEND_ROOT, "core")):
    _BACKEND_ROOT = os.getcwd()
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)

import httpx  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402

from core.auth import is_org_owner_id  # noqa: E402
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes  # noqa: E402
from core.config import settings  # noqa: E402
from core.containers import get_ecs_manager, get_workspace  # noqa: E402
from core.repositories import (  # noqa: E402
    admin_actions_repo,
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    messaging_repo,
    slack_installs_repo,
    update_repo,
    usage_repo,
    user_repo,
)
from core.repositories.paperclip_repo import PaperclipRepo, _org_create_claim_key  # noqa: E402
from core.services import clerk_admin, runtime_transport  # noqa: E402
from core.services.account_wipe import (  # noqa: E402
    _NO_PAPERCLIP_ROW,
    WipeReport,
    WipeStep,
    _archive_metronome_customer,
    _archive_paperclip_member_call,
    _delete_paperclip_company_call,
    _skip,
    _step,
    _wipe_company_stripe,
    cancel_open_workflows,
)
from core.services.billing_service import delete_stripe_customer  # noqa: E402
from core.services.litestream_replicas import delete_replica_prefix  # noqa: E402
from core.services.oauth_service import PURPOSE_HARNESS, revoke_user_oauth  # noqa: E402
from core.services.owner_secret_sweep import delete_owner_secrets  # noqa: E402
from core.services.paperclip_api_key import delete_owner_agent_key_secrets  # noqa: E402
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user  # noqa: E402

logger = logging.getLogger(__name__)

#: Orgs this script will never touch, whatever is typed. The founder org owns
#: the dogfood workspace every prod agent runs in; the internal team org is the
#: one every Lightbulb human is a member of. Both are recoverable only from a
#: PITR restore (see the 2026-09-15 incident where an admin wipe hard-deleted a
#: shared Paperclip company and replies 503'd for five hours).
FOUNDER_ORG_ID = "org_3H3viGAfq2SXc9dIdcatmGXg8Td"
TEAM_ORG_ID = "org_3JKSgGBDHBKMpj1RDCRJgldnh0h"
PROTECTED_ORG_IDS = frozenset({FOUNDER_ORG_ID, TEAM_ORG_ID})

_PAPERCLIP_TABLE = "paperclip-companies"


class Refusal(Exception):
    """A guard said no. Raised during validation, before any org's first step."""


# ---------------------------------------------------------------------------
# The four script-local primitives (see the module docstring for why they are
# here and not in core/). Each mirrors the posture of its neighbours in the
# deployed module it borrows from.
# ---------------------------------------------------------------------------


def _clerk_headers() -> dict[str, str] | None:
    """Auth header, or None when no key is configured (clerk_admin's own
    local-dev stub posture: every function there returns empty rather than
    raising)."""
    if not settings.CLERK_SECRET_KEY:
        return None
    return {"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"}


async def _clerk_org_with_member_count(org_id: str) -> tuple[dict | None, bool]:
    """``clerk_admin.get_organization`` plus ``?include_members_count=true``.

    Returns ``(org, reachable)``. Unlike the function it stands in for, a real
    **404** and a failure are NOT collapsed: ``(None, True)`` means Clerk
    answered and the org is genuinely gone, ``(None, False)`` means Clerk did
    not answer at all (network error, non-404 HTTP, unparseable body, or no
    key). ``check_refusals`` treats those completely differently — an
    already-deleted org still re-runs cleanly, while an unreachable Clerk is
    refused no matter what DDB shows, because every roster-shaped guard is
    then reading an empty list that means nothing.

    The count is the point: ``list_organization_members`` reports ANY error as
    an empty list, so this is the independent second read that catches a short
    roster.
    """
    headers = _clerk_headers()
    if headers is None:
        return None, False
    try:
        async with httpx.AsyncClient(timeout=clerk_admin._TIMEOUT_S) as client:
            response = await client.get(
                f"{clerk_admin._CLERK_API_BASE}/organizations/{org_id}",
                headers=headers,
                params={"include_members_count": "true"},
            )
    except Exception as e:  # noqa: BLE001 — mirrors get_organization
        logger.warning("wipe_org: clerk get organization %s network error: %s", org_id, e)
        return None, False
    if response.status_code == 404:
        return None, True
    if response.status_code >= 400:
        logger.warning("wipe_org: clerk get organization %s HTTP %s", org_id, response.status_code)
        return None, False
    try:
        return response.json(), True
    except ValueError:
        return None, False


#: One page of ``GET /v1/sessions``. Looped rather than single-shot: a session
#: left alive is precisely the re-provision this step exists to prevent.
_CLERK_SESSION_PAGE = 100


async def _revoke_sessions(user_id: str) -> str:
    """Sign one human out of every ACTIVE Clerk session.

    NOT ``clerk_admin.revoke_sessions``: that posts
    ``/v1/users/{id}/revoke_session``, which is not a route in Clerk's Backend
    API — the real pair is ``GET /v1/sessions`` + ``POST
    /v1/sessions/{id}/revoke``. It answers ``{"error": "http_404"}``, which its
    UX caller swallows, so nothing ever noticed; here it would fail step 0 for
    every member of every run while revoking nothing at all.

    404 on either call is success — the user, or that one session, is already
    gone, the same reading ``_clerk_delete_organization`` takes. Any other
    non-2xx raises, so a member this could not sign out is a recorded failure
    rather than a silent pass.
    """
    headers = _clerk_headers()
    if headers is None:
        return f"{user_id}: no Clerk key — nothing revoked"

    listed: list[str] = []
    revoked = 0
    async with httpx.AsyncClient(timeout=clerk_admin._TIMEOUT_S) as client:
        offset = 0
        while True:
            response = await client.get(
                f"{clerk_admin._CLERK_API_BASE}/sessions",
                headers=headers,
                params={
                    "user_id": user_id,
                    "status": "active",
                    "limit": _CLERK_SESSION_PAGE,
                    "offset": offset,
                },
            )
            if response.status_code == 404:
                return f"{user_id}: no Clerk user (already gone)"
            if response.status_code >= 400:
                raise RuntimeError(f"clerk list sessions for {user_id} failed: http_{response.status_code}")
            payload = response.json()
            # Clerk wraps collections as {data, total_count} on current API
            # versions and returned a bare list on older ones — accept both,
            # the same way list_user_organizations does.
            rows = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(rows, list):
                break
            listed.extend(s["id"] for s in rows if isinstance(s, dict) and s.get("id"))
            if len(rows) < _CLERK_SESSION_PAGE:
                break
            offset += _CLERK_SESSION_PAGE

        for session_id in listed:
            response = await client.post(f"{clerk_admin._CLERK_API_BASE}/sessions/{session_id}/revoke", headers=headers)
            if response.status_code == 404:
                continue  # already ended between the list and now
            if response.status_code >= 400:
                raise RuntimeError(f"clerk revoke session {session_id} failed: http_{response.status_code}")
            revoked += 1

    return f"{user_id}: revoked {revoked} of {len(listed)} active session(s)"


async def _clerk_delete_organization(org_id: str) -> None:
    """``DELETE /v1/organizations/{id}``.

    Same contract as ``clerk_admin.delete_user``, which is the closest thing
    the module has: 404 (already deleted) is success so a re-run after a
    partial wipe is safe, anything else RAISES so the step is recorded failed
    rather than a fake "ok", and an unset key is a no-op.

    Deleting the org deletes its memberships, not its members' user accounts —
    those are a separate, opt-in step.
    """
    headers = _clerk_headers()
    if headers is None:
        return
    async with httpx.AsyncClient(timeout=clerk_admin._TIMEOUT_S) as client:
        response = await client.delete(f"{clerk_admin._CLERK_API_BASE}/organizations/{org_id}", headers=headers)
    if response.status_code == 404:
        return
    if response.status_code >= 400:
        raise RuntimeError(f"clerk delete organization failed: http_{response.status_code} body={response.text}")


async def _company_row_user_ids(company_id: str) -> list[str]:
    """Every user_id naming ``company_id``, INCLUDING non-active rows.

    ``PaperclipRepo.get_user_ids_for_company`` filters to ``status="active"``
    because its caller wants a notification audience. This guard is asking a
    different question — "does any other workspace still lay claim to this
    company" — and a disabled row is a suspended member whose access can come
    back (``entitlement_teardown`` disables with a 30-day purge grace). Reusing
    the repo's own paginated GSI reader, so there is one pagination loop.
    """
    repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)
    user_ids: list[str] = []
    async for page in repo._query_pages(
        IndexName="by-company-id",
        KeyConditionExpression=Key("company_id").eq(company_id),
    ):
        user_ids.extend(item["user_id"] for item in page.get("Items", []) if item.get("user_id"))
    return user_ids


@dataclass
class _Inventory:
    """Everything read up front, for one org.

    Same posture as ``account_wipe._Identifiers``: it CARRIES values a later
    step can no longer read (a company id off a row an earlier step deletes)
    and it shapes the preview. It never decides that an idempotent delete may
    be skipped — that is how a wipe reports success over a row nobody touched.
    """

    org_id: str
    org: dict | None
    """The Clerk organization, or None when Clerk answered 404 (already
    deleted) OR could not be reached at all — ``clerk_reachable`` is what
    tells those apart."""

    members: list[dict]
    """Clerk memberships — ``{"user_id", "email", ...}``, see clerk_admin."""

    clerk_reachable: bool = True
    """False when the org read did not get an answer out of Clerk (network
    error, non-404 HTTP, no key). Every roster-shaped guard below reads an
    empty list in that state, so ``check_refusals`` stops the run outright
    rather than letting empty mean "nothing to protect"."""

    paperclip_rows: list[dict] = field(default_factory=list)
    """Raw ``paperclip-companies`` rows for this org, via the by-org-id GSI.
    One per MEMBER (rows are keyed by Clerk user id, not by org id)."""

    container: dict | None = None
    billing: dict | None = None
    user_rows: dict[str, dict] = field(default_factory=dict)

    member_orgs: dict[str, list[str]] = field(default_factory=dict)
    """Per member, the ids of EVERY Clerk org they belong to — read BEFORE this
    org is deleted, so the list containing ``org_id`` is the proof the lookup
    worked. An empty list is therefore "Clerk did not answer", not "no orgs",
    and the Clerk-user delete refuses on it."""

    @property
    def clerk_member_ids(self) -> list[str]:
        """Only the humans Clerk itself lists in this org. Narrower than
        ``member_ids`` on purpose — see step 0."""
        return [m["user_id"] for m in self.members if m.get("user_id")]

    @property
    def member_ids(self) -> list[str]:
        """Clerk members UNION Paperclip row owners.

        The union, not either alone: ``list_organization_members`` returns []
        both for "no members" and for any Clerk error, so a transient failure
        would otherwise hide a member from the multi-member refusal AND from
        the per-member teardown. A Paperclip row is durable local evidence
        that a human belongs here.
        """
        ids = [m["user_id"] for m in self.members if m.get("user_id")]
        for row in self.paperclip_rows:
            if row.get("user_id") and row["user_id"] not in ids:
                ids.append(row["user_id"])
        return ids

    def paperclip_row_for(self, user_id: str) -> dict | None:
        return next((r for r in self.paperclip_rows if r.get("user_id") == user_id), None)

    def email_for(self, user_id: str) -> str:
        """The member's address — Better Auth's copy, then Clerk's, then the
        users row. Empty only when NONE of the three knows it.

        Three sources because each fails independently and the answer feeds a
        safety guard: ``delete_paperclip_user``'s bootstrap-admin refusal keys
        on this string, so a "" makes that guard a silent no-op. Better Auth's
        copy comes first because it is the one that matches the Aurora row
        (``user.email`` is written once at sign-up and never updated).
        """
        row = self.paperclip_row_for(user_id) or {}
        if row.get("paperclip_email"):
            return row["paperclip_email"]
        clerk = next((m.get("email") or "" for m in self.members if m.get("user_id") == user_id), "")
        return clerk or (self.user_rows.get(user_id) or {}).get("email") or ""

    @property
    def company_ids(self) -> list[str]:
        """Every distinct Paperclip company named by this org's rows. More than
        one is a data defect, not a wipe target — see ``check_refusals``."""
        return list(dict.fromkeys(r["company_id"] for r in self.paperclip_rows if r.get("company_id")))

    @property
    def company_id(self) -> str:
        """The org's Paperclip company. Empty when the org has no rows (the
        member-less orphans) or only a bare seed-claim marker (company_id="")."""
        return self.company_ids[0] if self.company_ids else ""

    @property
    def runtime_host(self) -> str:
        return runtime_transport.host_for(self.container or {})

    @property
    def ecs_hosted(self) -> bool:
        """Has a live ECS service to drain. AgentCore rows never did."""
        return self.container is not None and self.runtime_host == "ecs"

    @property
    def has_ddb_footprint(self) -> bool:
        """Any durable row that proves this workspace still owns something."""
        return bool(self.container or self.billing or self.paperclip_rows)


async def read_inventory(org_id: str) -> _Inventory:
    repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)
    org, clerk_reachable = await _clerk_org_with_member_count(org_id)
    inv = _Inventory(
        org_id=org_id,
        org=org,
        clerk_reachable=clerk_reachable,
        members=await clerk_admin.list_organization_members(org_id),
        paperclip_rows=await repo.list_by_org_id(org_id),
        container=await container_repo.get_by_owner_id(org_id, consistent=True),
        billing=await billing_repo.get_by_owner_id(org_id, consistent_read=True),
    )
    for user_id in inv.member_ids:
        row = await user_repo.get(user_id)
        if row:
            inv.user_rows[user_id] = row
        # Read while this org still EXISTS: see _Inventory.member_orgs.
        inv.member_orgs[user_id] = [o["id"] for o in await clerk_admin.list_user_organizations(user_id) if o.get("id")]
    return inv


async def check_refusals(inv: _Inventory, *, allow_multi_member: bool) -> None:
    """Every hard refusal, in cheapest-first order. Raises ``Refusal``."""
    org_id = inv.org_id
    if not is_org_owner_id(org_id):
        raise Refusal(f"{org_id!r} is not a Clerk org id — this script only wipes org-owned workspaces")
    if org_id in PROTECTED_ORG_IDS:
        raise Refusal(f"{org_id} is a protected org (founder / internal team) — never wiped by this script")

    # Clerk has to be demonstrably reachable before its roster may be trusted.
    # get_organization returns None for 404, for a network error and for an
    # unset key alike, so "no org" alone proves nothing. members_count is the
    # independent second read: Clerk answered, and this is how many members it
    # says there are. A mismatch means list_organization_members came back
    # short — which it does silently, on ANY error.
    if not inv.clerk_reachable:
        # Not a 404 — Clerk never answered. The DDB footprint is NOT a tiebreak
        # here: an empty one would let a run proceed with members=[] that means
        # "no answer", disarming the multi-member, bootstrap-email and
        # cross-org-user guards all at once.
        raise Refusal(
            f"Clerk did not answer the organization read for {org_id} (network error, non-404 HTTP, "
            "or no CLERK_SECRET_KEY). Every roster guard reads an empty list in that state, so there "
            "is nothing to verify against — fix Clerk access and re-run"
        )
    if inv.org is None:
        if inv.has_ddb_footprint:
            raise Refusal(
                f"Clerk says organization {org_id} does not exist, but this workspace still has rows "
                "(container / billing / paperclip) — a deleted org with live residue. Resolve that by "
                "hand rather than wiping against a roster nobody can enumerate any more"
            )
        logger.warning(
            "wipe_org: %s has no Clerk organization and no DDB rows — treating as an already-torn-down "
            "workspace; nothing downstream depends on the roster",
            org_id,
        )
    else:
        count = inv.org.get("members_count")
        if count is None:
            raise Refusal(
                f"Clerk did not return members_count for {org_id} — cannot verify the member roster is "
                "complete, and list_organization_members reports an error as an empty list"
            )
        if int(count) != len(inv.members):
            raise Refusal(
                f"{org_id}: Clerk says {count} member(s) but the roster listed {len(inv.members)} — "
                "the member read came back short; refusing rather than wiping a roster we cannot see"
            )

    member_ids = inv.member_ids
    if len(member_ids) > 1 and not allow_multi_member:
        raise Refusal(
            f"{org_id} has {len(member_ids)} members ({', '.join(member_ids)}) — "
            "pass --allow-multi-member to wipe a shared workspace"
        )

    # An unknown address disarms delete_paperclip_user's bootstrap guard, which
    # tests the email string. Refuse rather than delete past a guard that
    # cannot fire.
    unknown = sorted(uid for uid in member_ids if not inv.email_for(uid))
    if unknown:
        raise Refusal(
            f"{org_id}: no email resolvable for {', '.join(unknown)} (not in Clerk, the paperclip row, or "
            "the users row) — the bootstrap-admin guard keys on the address and cannot fire without it"
        )

    protected = sorted({e for e in (inv.email_for(uid) for uid in member_ids) if e.strip().lower() in BOOTSTRAP_EMAILS})
    if protected:
        raise Refusal(
            f"{org_id} has a bootstrap Paperclip admin member ({', '.join(protected)}) — "
            "deleting it breaks the admin session every provision depends on"
        )

    if len(inv.company_ids) > 1:
        raise Refusal(
            f"{org_id}'s paperclip rows name {len(inv.company_ids)} different companies "
            f"({', '.join(inv.company_ids)}) — that is a data defect; resolve it by hand rather than "
            "letting this script delete whichever one it happened to read first"
        )

    # Never delete a Paperclip company a SECOND workspace also names. Same
    # signal ``account_wipe._shared_company`` uses (the by-company-id GSI), but
    # WITHOUT its active-only filter: a disabled row is a member whose access
    # can come back (entitlement_teardown disables with a 30-day grace), so it
    # still counts as "somebody else lives here".
    if inv.company_id:
        outsiders = sorted(set(await _company_row_user_ids(inv.company_id)) - set(member_ids))
        if outsiders:
            raise Refusal(
                f"{org_id}'s Paperclip company {inv.company_id} is also named by row(s) for "
                f"{', '.join(outsiders)} — refusing to delete a company two workspaces share"
            )


# ---------------------------------------------------------------------------
# The one step runner: preview and execute share it, so they cannot diverge.
# ---------------------------------------------------------------------------


async def _do(
    steps: list[WipeStep],
    *,
    system: str,
    action: str,
    execute: bool,
    present: bool,
    detail: str,
    fn: Callable[[], Awaitable[str | None]],
) -> None:
    """Record one teardown step, and run it only when ``execute``.

    ``present=False`` means the inventory found nothing — reported "skipped"
    with ``detail`` saying so. ``present=True`` with ``execute=False`` is the
    read-only "would_delete" line. Only the third case calls ``fn``, through
    ``account_wipe._step``, which isolates the failure and lets the remaining
    steps run.
    """
    if not present:
        _skip(steps, system, action, detail)
    elif not execute:
        steps.append(WipeStep(system=system, action=action, status="would_delete", detail=detail))
    else:
        await _step(steps, system=system, action=action, fn=fn)


def _status_of(steps: list[WipeStep], system: str) -> str:
    return next((s.status for s in steps if s.system == system), "skipped")


async def wipe_org(inv: _Inventory, *, execute: bool, delete_clerk_users: bool) -> WipeReport:
    """The ordered teardown. Mirrors ``account_wipe.wipe_account``'s step order
    and system names wherever the resource is the same, so a reader who knows
    one knows the other."""
    org_id = inv.org_id
    steps: list[WipeStep] = []
    env = settings.ENVIRONMENT

    # --- Step 0: stop the workspace regrowing under the teardown -----------
    # Every member's /container/status poll re-runs ensure_user_ready, which
    # re-provisions. Killing their Clerk sessions first makes that poll 401
    # instead of racing steps 1-8.
    #
    # Scoped to inv.clerk_member_ids, NOT the Clerk-UNION-Paperclip member_ids
    # every other per-member step uses. The union exists so a stale paperclip
    # row cannot hide a member from the guards; signing someone out is the one
    # action where that breadth is wrong. A row left behind by a human who
    # already left this workspace would otherwise end THEIR sessions in the
    # other workspaces they are still working in.
    for user_id in inv.clerk_member_ids:
        await _do(
            steps,
            system="clerk_sessions",
            action="revoke_sessions",
            execute=execute,
            present=True,
            detail=f"user_id={user_id}",
            fn=lambda user_id=user_id: _revoke_sessions(user_id),
        )

    # --- Step 1: per Clerk member ----------------------------------------
    # Membership archive BEFORE the Better Auth delete, not after: archiving
    # makes Paperclip reassign the member's issues, which is what leaves the
    # `"user"` row FK-clean for the DELETE in the next step.
    for user_id in inv.member_ids:
        row = inv.paperclip_row_for(user_id)
        await _do(
            steps,
            system="paperclip_membership",
            action="archive_member",
            execute=execute,
            present=row is not None,
            detail=(f"user_id={user_id}" if row else f"user_id={user_id}: no paperclip-companies row"),
            fn=lambda user_id=user_id: _archive_member(user_id),
        )
        await _do(
            steps,
            system="paperclip_aurora_user",
            action="delete_paperclip_user",
            execute=execute,
            present=bool((row or {}).get("paperclip_user_id")),
            detail=(
                f"user_id={user_id} paperclip_user_id={(row or {}).get('paperclip_user_id')}"
                if (row or {}).get("paperclip_user_id")
                else f"user_id={user_id}: no paperclip_user_id on the row"
            ),
            fn=lambda user_id=user_id, row=row: _delete_better_auth_user(
                user_id, row or {}, inv.email_for(user_id), env
            ),
        )
        await _do(
            steps,
            system="user_row",
            action="user_repo.delete",
            execute=execute,
            present=user_id in inv.user_rows,
            detail=f"user_id={user_id}" if user_id in inv.user_rows else f"user_id={user_id}: no users row",
            fn=lambda user_id=user_id: _delete_user_row(user_id),
        )

    # --- Step 2: the Paperclip company (hard delete) ----------------------
    # A missing row is NOT proof the Paperclip side is gone — the handle to it
    # may simply be absent (partial provision, prior partial wipe, or a re-run
    # after step 8 deleted the rows while THIS step failed). Reported loudly
    # with account_wipe's own wording and counted toward a non-zero exit.
    if not inv.company_id:
        logger.warning("wipe_org: %s — %s", org_id, _NO_PAPERCLIP_ROW)
    await _do(
        steps,
        system="paperclip_company",
        action="delete_company",
        execute=execute,
        present=bool(inv.company_id),
        detail=f"company_id={inv.company_id}" if inv.company_id else _NO_PAPERCLIP_ROW,
        fn=lambda: _delete_company(inv.company_id),
    )

    # --- Step 3: the runtime ----------------------------------------------
    # Gate-token rows FIRST: the table is keyed on the token hash with no owner
    # index, so the containers row is the only handle on them — and the runtime
    # step destroys that row. A stranded row is unreachable forever, at
    # denied=False. (The row's own ALL_OLD image is swept again inside the
    # runtime step, which closes the mint-in-between window.)
    snapshot_hashes = owner_gate_token_hashes(inv.container)
    await _do(
        steps,
        system="bedrock_gate_tokens",
        action="delete_gate_token_rows",
        execute=execute,
        present=bool(snapshot_hashes),
        detail=(
            f"{len(snapshot_hashes)} gate-token row(s)"
            if snapshot_hashes
            else "no bedrock-gate token hash on containers row"
        ),
        fn=lambda: _delete_gate_tokens(org_id, snapshot_hashes),
    )

    # DRAIN BEFORE DELETE. delete_user_service issues desiredCount=0 and
    # delete_service(force=True) back to back and returns — it does not wait.
    # Worse, a DELETED service describes as absent while its last task is still
    # STOPPING (_tasks_fully_stopped_blocking reads ServiceNotFoundException as
    # drained), so waiting afterwards can return True over a container that is
    # still running its 120s stopTimeout — exactly when litestream performs its
    # FINAL replica sync. Deleting the replica prefix in that window lets the
    # late upload repopulate it with live OAuth refresh tokens, which the next
    # provision would restore. Scaling to zero first gives the drain wait a
    # service that is genuinely going away and a signal that means what it says.
    await _do(
        steps,
        system="container_drain",
        action="stop_and_wait_until_drained",
        execute=execute,
        present=inv.ecs_hosted,
        detail=(
            f"scale {(inv.container or {}).get('service_name', '')} to 0 and wait for the task to STOP"
            if inv.ecs_hosted
            else f"runtime_host={inv.runtime_host}: no ECS service to drain"
        ),
        fn=lambda: _drain_container(org_id),
    )
    # A failed drain must still block the replica delete — _step keeps going
    # after a failure by design, so the decision is carried here.
    drain_ok = (not inv.ecs_hosted) or _status_of(steps, "container_drain") in ("ok", "would_delete")

    # ONE call for both hosts: delete_user_service branches on runtime_host and
    # for an AgentCore row runs teardown_hermes_runtime (StopRuntimeSession,
    # DeleteAgentRuntime, revoke the bearer, delete the boot secret) plus — because
    # we pass delete_hermes_profiles=True, the terminal-caller flag — the whole
    # <org>/hermes/ S3 prefix. For an ECS row it deletes the service,
    # deregisters the task def, deletes the EFS access point, and sweeps
    # channel-links. Both paths delete the containers row and reap the owner's
    # Bedrock footprint (AIPs + IAM role) at the shared seam.
    await _do(
        steps,
        system="ecs_service",
        action="delete_user_service",
        execute=execute,
        present=inv.container is not None,
        detail=(
            f"runtime_host={inv.runtime_host} "
            f"service_name={(inv.container or {}).get('service_name', '')} "
            f"agentcore_runtime_arn={(inv.container or {}).get('agentcore_runtime_arn', '')} "
            "(+ containers row, channel-links, Bedrock AIPs + IAM role, and — on agentcore — "
            "the runtime, boot secret and <org>/hermes/ S3 profiles)"
            if inv.container is not None
            else "no containers row"
        ),
        fn=lambda: _delete_runtime(org_id, inv.runtime_host),
    )

    workspace_path = get_workspace().user_path(org_id)
    await _do(
        steps,
        system="efs_workspace_dir",
        action="delete_user_dir",
        execute=execute,
        present=workspace_path.exists(),
        detail=str(workspace_path) if workspace_path.exists() else "directory does not exist",
        fn=lambda: _delete_workspace_dir(org_id),
    )

    await _do(
        steps,
        system="litestream_replicas",
        action="delete_replica_prefix",
        execute=execute,
        present=bool(settings.LITESTREAM_BUCKET),
        detail=(
            f"s3://{settings.LITESTREAM_BUCKET}/{org_id}/"
            if settings.LITESTREAM_BUCKET
            else "LITESTREAM_BUCKET unset (EFS layout)"
        ),
        fn=lambda: _delete_replicas(org_id, drained=drain_ok),
    )

    # --- Step 4: Metronome (AIPs + gate token already reaped above) -------
    metronome_customer_id = (inv.billing or {}).get("metronome_customer_id") or ""
    await _do(
        steps,
        system="metronome",
        action="archive_customer",
        execute=execute,
        present=bool(metronome_customer_id),
        detail=metronome_customer_id or "no metronome_customer_id on billing row",
        fn=lambda: _archive_metronome(metronome_customer_id),
    )

    # --- Step 5: credentials ----------------------------------------------
    # Deliberately NOT routers/connections.py::_disconnect_teardown: that is a
    # per-provider disconnect that patches the owner's openclaw.json and removes
    # materialized files from EFS / Hermes S3 profiles, both of which step 3 has
    # already destroyed wholesale. What is left after step 3 is the durable
    # credential material, and account_wipe's own trio is exactly that sweep.
    # Google's remote revoke does not run on either path — neither this nor
    # _disconnect_teardown calls it (see revoke_user_oauth's docstring: our
    # stored tokens are deleted, the upstream grant is not).
    #
    # Open provisioning workflows are CANCELLED FIRST — never deprovisioned
    # (spec §3.6) — so a reader advancing a workflow mid-wipe cannot land a
    # fresh secret after the sweeps below have already run.
    await _do(
        steps,
        system="provisioning",
        action="cancel_open_workflows",
        execute=execute,
        present=True,
        detail="close every non-terminal provisioning workflow (never deprovisions)",
        fn=lambda: _cancel_workflows(org_id),
    )
    await _do(
        steps,
        system="owner_secrets",
        action="delete_owner_secrets",
        execute=execute,
        present=True,
        detail="Secrets Manager: BYOK LLM keys, integration credentials, per-owner OAuth apps",
        fn=lambda: _delete_secrets(org_id),
    )
    await _do(
        steps,
        system="paperclip_agent_keys",
        action="delete_owner_agent_key_secrets",
        execute=execute,
        present=True,
        detail="Secrets Manager: per-agent Paperclip API keys",
        fn=lambda: _delete_agent_key_secrets(org_id),
    )
    await _do(
        steps,
        system="integrations",
        action="delete_all_for_owner",
        execute=execute,
        present=True,
        detail="all integrations rows for owner",
        fn=lambda: _delete_integrations(org_id),
    )
    await _do(
        steps,
        system="oauth_tokens",
        action="revoke_user_oauth",
        execute=execute,
        present=True,
        detail="stored ChatGPT OAuth tokens, provider + harness rows",
        fn=lambda: _revoke_oauth(org_id),
    )

    # --- Step 6: billing ---------------------------------------------------
    stripe_customer_id = (inv.billing or {}).get("stripe_customer_id") or ""
    await _do(
        steps,
        system="stripe",
        action="delete_stripe_customer",
        execute=execute,
        present=bool(stripe_customer_id),
        detail=stripe_customer_id or "no stripe_customer_id on billing row",
        fn=lambda: _delete_stripe(org_id, stripe_customer_id),
    )
    # The company's OWN Stripe account is never deleted — it is their business
    # account. This only releases the platform's authorization on it.
    company_account_id = (inv.billing or {}).get("company_stripe_account_id") or ""
    company_stripe_outcome: list[WipeStep] = []
    await _do(
        steps,
        system="stripe",
        action="deauthorize_company_account",
        execute=execute,
        present=bool(company_account_id),
        detail=company_account_id or "no company_stripe_account_id on billing row",
        fn=lambda: _deauthorize_company_stripe(org_id, company_stripe_outcome),
    )
    # _wipe_company_stripe decides its own status, and it can legitimately
    # SKIP: it re-reads the billing row strongly consistent, so a binding that
    # vanished between our snapshot and that read means nothing was revoked.
    # Reporting a no-op as "ok" would claim the platform let go of an account
    # it never touched.
    if company_stripe_outcome and company_stripe_outcome[-1].status == "skipped" and steps[-1].status == "ok":
        steps[-1] = replace(steps[-1], status="skipped", detail=company_stripe_outcome[-1].detail)
    await _do(
        steps,
        system="billing_row",
        action="billing_repo.delete",
        execute=execute,
        present=inv.billing is not None,
        detail="billing-accounts row" if inv.billing is not None else "no billing-accounts row",
        fn=lambda: _delete_billing_row(org_id),
    )

    # --- Step 7: messaging -------------------------------------------------
    # The owner partition holds the room catalog, the private user-stream
    # mappings and the OWNER#<org>/RUN# ledger rows; each room partition holds
    # its members and messages.
    room_ids = await messaging_repo.list_owner_room_ids(org_id)
    await _do(
        steps,
        system="messaging",
        action="delete_owner_rooms_and_streams",
        execute=execute,
        present=True,
        detail=f"{len(room_ids)} owned room partition(s), then owner catalog, runs and user-stream mappings",
        fn=lambda: _delete_messaging(org_id),
    )

    # --- Step 8: the remaining owner-scoped rows ---------------------------
    # containers row: already gone with step 3. admin-actions: NEVER deleted.
    await _do(
        steps,
        system="usage_counters",
        action="delete_all_for_owner",
        execute=execute,
        present=True,
        detail="all usage-counter rows for owner",
        fn=lambda: _delete_usage(org_id),
    )
    await _do(
        steps,
        system="pending_updates",
        action="delete_all_for_owner",
        execute=execute,
        present=True,
        detail="all pending-update rows for owner",
        fn=lambda: _delete_updates(org_id),
    )
    await _do(
        steps,
        system="paperclip_row",
        action="paperclip_repo.delete",
        execute=execute,
        present=True,
        detail=(
            f"{len(inv.paperclip_rows)} paperclip-companies row(s), "
            "the __pending_brief__ item and the __org_create_claim__ item"
        ),
        fn=lambda: _delete_paperclip_rows(org_id, [r["user_id"] for r in inv.paperclip_rows if r.get("user_id")]),
    )
    await _do(
        steps,
        system="company_slug",
        action="delete_for_owner",
        execute=execute,
        present=True,
        detail="slug reservation, if any",
        fn=lambda: _delete_slug(org_id),
    )
    await _do(
        steps,
        system="agent_email_addresses",
        action="delete_all_for_owner",
        execute=execute,
        present=True,
        detail="every {agent}@{slug} address routing inbound SES mail at this owner",
        fn=lambda: _delete_agent_emails(org_id),
    )
    await _do(
        steps,
        system="slack_installs",
        action="delete_all_for_owner",
        execute=execute,
        present=True,
        detail="every Slack install bound to this owner",
        fn=lambda: _delete_slack_installs(org_id),
    )

    # --- Step 9: Clerk — the organization, then (opt-in) its users ---------
    # Last, and in this order: while the org lives, a member's next
    # /container/status poll re-provisions everything above it.
    await _do(
        steps,
        system="clerk_org",
        action="delete_organization",
        execute=execute,
        present=True,
        detail=f"{org_id} ({(inv.org or {}).get('name') or 'name unknown / already deleted'})",
        fn=lambda: _delete_clerk_org(org_id),
    )
    for user_id in inv.member_ids:
        await _clerk_user_step(steps, inv, user_id, execute=execute, delete_clerk_users=delete_clerk_users)

    return WipeReport(owner_id=org_id, steps=steps, dry_run=not execute)


async def _clerk_user_step(
    steps: list[WipeStep],
    inv: _Inventory,
    user_id: str,
    *,
    execute: bool,
    delete_clerk_users: bool,
) -> None:
    """Queue (or refuse) one member's Clerk-user delete.

    A Clerk user is a HUMAN, not a workspace resource: the same person may own
    or belong to another workspace, and deleting their account here would take
    that with it. So the delete is refused outright for anyone who belongs to
    any org besides this one — and equally for anyone whose org list we could
    not read, because ``list_user_organizations`` reports an error as an empty
    list and this lookup ran while they were still a member of ``inv.org_id``.
    Seeing that id is the proof the lookup worked; not seeing it means the
    answer is unusable, and an unusable answer is never grounds to delete a
    person's account.

    The two refusals are emitted directly rather than through ``_do`` because a
    guard keeping a resource on purpose is ``account_wipe``'s "refused" status
    — neither a success nor a failure, and never a delete — which ``_do`` has
    no case for. The delete itself still goes through ``_do``.
    """
    if not delete_clerk_users:
        _skip(steps, "clerk_user", "delete_user", f"{user_id}: kept (pass --delete-clerk-users to delete)")
        return

    orgs = inv.member_orgs.get(user_id) or []
    if inv.org_id not in orgs:
        steps.append(
            WipeStep(
                "clerk_user",
                "delete_user",
                "refused",
                f"{user_id}: could not confirm this human's org memberships — Clerk listed "
                f"{len(orgs)} org(s) and none of them is {inv.org_id}, which they are a member of. "
                "Keeping the user.",
            )
        )
        return

    others = sorted(o for o in orgs if o != inv.org_id)
    if others:
        steps.append(
            WipeStep(
                "clerk_user",
                "delete_user",
                "refused",
                f"{user_id}: member of {len(others)} other org(s) ({', '.join(others)}) — "
                "deleting this Clerk user would take their other workspace(s) with it. Keeping the user.",
            )
        )
        return

    await _do(
        steps,
        system="clerk_user",
        action="delete_user",
        execute=execute,
        present=True,
        detail=f"{user_id}: sole org is {inv.org_id}",
        fn=lambda: _delete_clerk_user(user_id),
    )


# ---------------------------------------------------------------------------
# One thin wrapper per collaborator: each returns the report detail string.
# Module-level so a test can monkeypatch the name it wants without reaching
# into a closure. They exist because ``_do`` takes a zero-argument async
# callable and a Python lambda cannot contain ``await``.
# ---------------------------------------------------------------------------


async def _archive_member(user_id: str) -> str:
    await _archive_paperclip_member_call(user_id)
    return f"membership archived for {user_id}"


async def _delete_better_auth_user(user_id: str, row: dict, email: str, env: str) -> str:
    paperclip_user_id = row["paperclip_user_id"]
    # ``email`` is only ever read by delete_paperclip_user's own bootstrap-admin
    # refusal — it matches by primary key, not by address.
    await delete_paperclip_user(paperclip_user_id=paperclip_user_id, email=email, env=env)
    return f"{user_id}: paperclip_user_id={paperclip_user_id} deleted"


async def _delete_user_row(user_id: str) -> str:
    await user_repo.delete(user_id)
    return f"{user_id}: users row deleted"


async def _delete_company(company_id: str) -> str:
    await _delete_paperclip_company_call(company_id)
    return f"company_id={company_id}"


async def _delete_gate_tokens(org_id: str, snapshot_hashes: list[str]) -> str:
    # Re-read strongly consistent and delete the UNION: the inventory read is
    # eventually consistent and predates the Paperclip steps, so a mint in that
    # window can be missing from it — while a mint that superseded and pruned a
    # hash between the two reads is named ONLY by the snapshot. Deleting a hash
    # twice is a free no-op; missing one is permanent.
    fresh = await container_repo.get_by_owner_id(org_id, consistent=True)
    hashes = list(dict.fromkeys([*snapshot_hashes, *owner_gate_token_hashes(fresh)]))
    deleted = await delete_gate_token_rows(hashes)
    return f"deleted {deleted} of {len(hashes)} gate-token row(s)"


async def _drain_container(org_id: str) -> str:
    ecs = get_ecs_manager()
    await ecs.stop_user_service(org_id, reason="wipe_org")
    # Budget exceeds the container's 120s stopTimeout so a normal full-length
    # drain (litestream's final replica sync) does not read as a timeout.
    if not await ecs.wait_until_drained(org_id, timeout_s=180.0):
        raise RuntimeError("container did not drain within 180s — a litestream writer may still be live")
    return "scaled to 0 and every task reached STOPPED"


async def _delete_runtime(org_id: str, runtime_host: str) -> str:
    row_at_death = await get_ecs_manager().delete_user_service(org_id, delete_hermes_profiles=True)
    detail = f"runtime_host={runtime_host}: runtime, per-owner resources, containers row and Bedrock footprint removed"
    # FINAL SWEEP — the last instant any gate-token hash is reachable: this is
    # the DELETE's own ALL_OLD image, so it covers a hash minted after the
    # strongly-consistent re-read above, and every hash at all if that step failed.
    late_hashes = owner_gate_token_hashes(row_at_death)
    swept = await delete_gate_token_rows(late_hashes) if late_hashes else 0
    if swept:
        detail += f" (+ swept {swept} gate-token row(s) still named on the row at delete time)"
    return detail


async def _delete_workspace_dir(org_id: str) -> str:
    workspace = get_workspace()
    workspace.delete_user_dir(org_id)
    return str(workspace.user_path(org_id))


async def _delete_replicas(org_id: str, *, drained: bool) -> str:
    # The drain is a PRIOR step; refuse if it did not succeed. A visible,
    # retryable failure beats deleting a prefix a live writer can repopulate
    # with the owner's rotated OAuth refresh tokens.
    if not drained:
        raise RuntimeError("container did not drain; refusing to delete replicas while a writer may still be live")
    if not await delete_replica_prefix(org_id):
        raise RuntimeError("replica prefix delete failed")
    return f"s3 prefix {org_id}/"


async def _archive_metronome(customer_id: str) -> str:
    await _archive_metronome_customer(customer_id)
    return customer_id


async def _cancel_workflows(org_id: str) -> str:
    cancelled, open_count = await cancel_open_workflows(org_id)
    return f"cancelled {cancelled} of {open_count} open provisioning workflow(s)"


async def _delete_secrets(org_id: str) -> str:
    names = await delete_owner_secrets(org_id)
    return f"deleted {len(names)} owner secret(s)"  # names are secret paths — count only


async def _delete_agent_key_secrets(org_id: str) -> str:
    return f"deleted {await delete_owner_agent_key_secrets(org_id)} per-agent key secret(s)"


async def _delete_integrations(org_id: str) -> str:
    providers = await integrations_repo.delete_all_for_owner(org_id)
    return f"deleted {len(providers)} integration row(s): {', '.join(providers) or 'none'}"


async def _revoke_oauth(org_id: str) -> str:
    await revoke_user_oauth(user_id=org_id)
    # The coding-harness grant is a SECOND row under a suffixed key; the bare
    # delete above never touches it.
    await revoke_user_oauth(user_id=org_id, purpose=PURPOSE_HARNESS)
    return "provider + harness oauth rows deleted (grant NOT revoked upstream at OpenAI)"


async def _delete_stripe(org_id: str, stripe_customer_id: str) -> str:
    return f"stripe_customer_id={stripe_customer_id} deleted={await delete_stripe_customer(org_id)}"


async def _deauthorize_company_stripe(org_id: str, outcome: list[WipeStep]) -> str:
    """account_wipe's own step 8b, which appends its result to a step list.

    Run it against the caller's ``outcome`` list and re-raise its failure so
    this script's single ``_do`` seam reports it like every other step. The
    caller reads ``outcome`` afterwards because ``_step`` can only produce
    ok/failed, and this collaborator has a third answer — ``skipped``.
    """
    await _wipe_company_stripe(outcome, owner_id=org_id)
    last = outcome[-1]
    if last.status == "failed":
        raise RuntimeError(last.error or "company stripe deauthorize failed")
    return last.detail


async def _delete_billing_row(org_id: str) -> str:
    await billing_repo.delete(org_id)
    return "billing-accounts row deleted"


async def _delete_messaging(org_id: str) -> str:
    room_ids = await messaging_repo.list_owner_room_ids(org_id)
    deleted = 0
    # Keep the owner catalog until every room partition is gone, so a partial
    # failure retries from the same complete room-id inventory.
    for room_id in room_ids:
        meta = await messaging_repo.get_room(room_id)
        if meta is not None and meta.get("ownerId") != org_id:
            raise RuntimeError("messaging owner catalog referenced a room owned elsewhere")
        deleted += await messaging_repo.delete_room_partition(room_id)
    deleted += await messaging_repo.delete_owner_partition(org_id)
    return f"deleted {len(room_ids)} room partition(s) and {deleted} messaging row(s)"


async def _delete_usage(org_id: str) -> str:
    return f"deleted {await usage_repo.delete_all_for_owner(org_id)} usage row(s)"


async def _delete_updates(org_id: str) -> str:
    return f"deleted {await update_repo.delete_all_for_owner(org_id)} pending-update row(s)"


async def _delete_paperclip_rows(org_id: str, user_ids: list[str]) -> str:
    repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)
    for user_id in user_ids:
        await repo.delete(user_id)
    # Two more items live in the SAME table under synthetic keys, both keyed by
    # OWNER id rather than by member: the funnel answer (the user's own typed
    # words, which deliberately outlives a reprovision) and the org-create claim.
    # account_wipe could never reach the claim — it only exists on the org
    # branch of ensure_user_ready, which its personal-only wipe never saw.
    brief = await repo.delete_pending_brief(org_id)
    await repo.delete(_org_create_claim_key(org_id))
    return f"deleted {len(user_ids)} row(s), pending_brief={brief}, org_create_claim"


async def _delete_slug(org_id: str) -> str:
    slug = await company_slug_repo.delete_for_owner(org_id)
    return f"released slug {slug}" if slug else "no slug reservation"


async def _delete_agent_emails(org_id: str) -> str:
    addresses = await agent_email_repo.delete_all_for_owner(org_id)
    return f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}"


async def _delete_slack_installs(org_id: str) -> str:
    teams = await slack_installs_repo.delete_all_for_owner(org_id)
    return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"


async def _delete_clerk_org(org_id: str) -> str:
    await _clerk_delete_organization(org_id)
    return org_id


async def _delete_clerk_user(user_id: str) -> str:
    await clerk_admin.delete_user(user_id)
    return user_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def unresolved_paperclip(report: WipeReport) -> bool:
    """Whether the Paperclip company could not be resolved at all. NOT the same
    as "this owner had no Paperclip side": the handle (the DDB row) may simply
    be missing, including on a re-run where step 8 deleted the rows while step
    2 failed. Drives a non-zero exit so it cannot be read as a clean wipe."""
    return any(s.system == "paperclip_company" and s.detail == _NO_PAPERCLIP_ROW for s in report.steps)


def print_report(report: WipeReport) -> None:
    mode = "PREVIEW (nothing deleted)" if report.dry_run else "EXECUTED"
    print(f"\n=== {report.owner_id} — {mode} ===")
    for s in report.steps:
        line = f"  [{s.status:>12}] {s.system}.{s.action}"
        if s.detail:
            line += f" — {s.detail}"
        if s.error:
            line += f" — ERROR: {s.error}"
        print(line)
    statuses = sorted({s.status for s in report.steps})
    print("  -- " + ", ".join(f"{sum(1 for s in report.steps if s.status == st)} {st}" for st in statuses))
    if unresolved_paperclip(report):
        print(f"  !! WARNING: {_NO_PAPERCLIP_ROW}")


async def _audit(report: WipeReport, elapsed_ms: int) -> None:
    """One admin-actions row per executed org. Best-effort: the teardown has
    already run, so an audit-write failure must not mask it (same posture as
    the admin router's wipe route)."""
    has_failures = any(s.status == "failed" for s in report.steps)
    try:
        await admin_actions_repo.create(
            admin_user_id="script:wipe_org",
            target_user_id=report.owner_id,
            action="wipe_org",
            payload=report.to_dict(),
            result="partial_failure" if has_failures else "success",
            audit_status="written",
            http_status=200,
            elapsed_ms=elapsed_ms,
            error_message=None,
            user_agent="scripts/wipe_org.py",
            ip="",
        )
    except Exception:  # noqa: BLE001 — never mask a teardown that already ran
        logger.critical("WIPE_ORG_AUDIT_PANIC target=%s report=%s", report.owner_id, report.to_dict())


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--org", action="append", required=True, metavar="ORG_ID", help="repeatable")
    parser.add_argument("--execute", action="store_true", help="actually delete (default: read-only preview)")
    parser.add_argument("--delete-clerk-users", action="store_true", help="also delete each member's Clerk user")
    parser.add_argument("--allow-multi-member", action="store_true", help="permit an org with more than one member")
    args = parser.parse_args(argv)

    # Which Aurora cluster, which Secrets Manager prefix, which everything. An
    # unset ENVIRONMENT silently defaults to "dev" all over this codebase — on
    # a laptop that means a prod-shaped command quietly hitting dev, and in the
    # prod task it would mean the reverse. Name it or do not run.
    if not settings.ENVIRONMENT:
        print("REFUSED: ENVIRONMENT is unset — run this inside the backend task, not on a laptop", file=sys.stderr)
        return 2

    # Validate EVERY org before touching ANY of them: a run starts clean or does nothing.
    inventories: list[_Inventory] = []
    refusals: list[str] = []
    for org_id in args.org:
        try:
            # A non-org id is refused without spending a single read on it.
            inv = await read_inventory(org_id) if is_org_owner_id(org_id) else _Inventory(org_id, None, [])
            await check_refusals(inv, allow_multi_member=args.allow_multi_member)
        except Refusal as r:
            refusals.append(str(r))
            continue
        inventories.append(inv)
    if refusals:
        for r in refusals:
            print(f"REFUSED: {r}", file=sys.stderr)
        return 2

    problems = 0
    for inv in inventories:
        started = time.monotonic()
        report = await wipe_org(inv, execute=args.execute, delete_clerk_users=args.delete_clerk_users)
        print_report(report)
        if args.execute:
            await _audit(report, int((time.monotonic() - started) * 1000))
        problems += sum(1 for s in report.steps if s.status == "failed")
        problems += 1 if unresolved_paperclip(report) else 0
    if not args.execute:
        print("\nPreview only — nothing was deleted. Re-run with --execute to commit.")
    return 1 if problems else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
