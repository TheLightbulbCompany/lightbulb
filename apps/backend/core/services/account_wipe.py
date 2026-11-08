"""Admin Account-Wipe orchestrator.

(docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md)

Two entry points:

* ``preview_wipe(owner_id)`` — READ-ONLY. Reads the owner's identifier rows
  (containers/paperclip-companies/billing-accounts/users) and, for the EFS
  workspace directory, does a filesystem existence check. Emits a
  ``WipeReport`` of ``would_delete``/``skipped`` steps. Calls NO delete
  method — safe to call as many times as an admin wants before committing.

* ``wipe_account(owner_id, *, actor_admin_id)`` — the ordered teardown (spec
  "complete teardown map", steps 1-12). Reads the SAME identifier rows up
  front so every later step already has the ARNs/ids it needs off a row an
  earlier step deletes — but that snapshot decides no DELETE (see
  ``_Identifiers``: it is stale by construction, and the gate-token step
  re-reads for exactly that reason). Then runs each teardown step in its own
  try/except. A step that raises is recorded
  ``status="failed"`` with the error and execution CONTINUES — this
  partial-failure tolerance is the load-bearing property: a Paperclip
  timeout must not block the Clerk delete (step 12, always last, so a
  killed account can never regrow on the next ``/container/status`` poll).

Refuses org owner ids (``OrgWipeUnsupportedError``) — the target is always a
Clerk USER. Wiping a whole workspace (N Paperclip rows + N Better Auth users,
one shared company) is ``core.services.workspace_wipe`` — the workspace-keyed
orchestrator that snapshots by ORG id; see its module docstring and the design
spec's "Out of scope: Org accounts" follow-up note. When a user-keyed wipe
finds no containers row but the member belongs to a workspace,
``_org_hint_for_missing_container`` names the org and points at it.

A human's wipe NEVER deletes a Paperclip company: one workspace = one
company, owned by the workspace row, and the human's own paperclip-companies
row is their Better Auth identity only. For every workspace the human belongs
to (Clerk memberships, read strict before any step) the company delete is
REFUSED (status ``"refused"``) and only this human's membership is archived
(``"paperclip_membership"``). On 2026-09-15 an admin wipe of a joined member
hard-deleted the founder workspace's company — every agent, issue and routine
— under the old per-user row shape.

Step-to-spec-row mapping. The design spec's teardown map lists 12 rows;
this orchestrator emits one ``WipeStep`` per row EXCEPT rows 5 and 11,
which fold into sibling steps because no standalone collaborator exists
for them (see the inline notes at those call sites):

  spec #1  -> "paperclip_company"     (always "refused" or "skipped": a company is
              the workspace's; each workspace gets
              "paperclip_membership" -> PaperclipRepo.remove_member)
  spec #2  -> "paperclip_aurora_user" (paperclip_user_delete.delete_paperclip_user)
  (no spec row) -> "bedrock_gate_tokens" (bedrock_gate_tokens.delete_gate_token_rows)
  (no spec row) -> "provisioning" (provisioning_workflow_repo.transition -> "cancelled")
  spec #3  -> "runtime"               (hermes_provisioning.delete_workspace_runtime)
  spec #4  -> folded into "runtime" (the S3 profile prefix IS the owner's content
              on this transport; there is no EFS tree left)
  spec #5  -> folded into "runtime" (no per-owner EFS access point left either)
  spec #6-7-> folded into "runtime" (bedrock_aip.reap_owner_bedrock_resources)
  spec #8  -> folded into "runtime" (bedrock_aip.reap_owner_bedrock_resources)
  spec #9  -> "metronome"             (MetronomeClient.archive_customer)
  spec #10 -> "stripe"                (billing_service.delete_stripe_customer)
  spec #11 -> "usage_counters" / "messaging" / "paperclip_row" /
              "billing_row" / "user_row"
              (containers row + channel-links already folded into
              "runtime" — see note below; "paperclip_row" also carries the
              owner's ``__pending_brief__:`` item, which lives in the SAME
              table under a prefixed key)
  spec #12 -> "clerk_user"            (clerk_admin.delete_user) -- ALWAYS LAST

KNOWN GAP — NOT YET COVERED (honest, not "complete"): this orchestrator does
NOT wipe the ``integrations`` DDB table, the ``oauth-tokens`` table, the
``api-keys`` table, or the Secrets-Manager ``integration_secret_store``
(BYOK API keys + ChatGPT OAuth token material). No collaborator exists yet
for any of them. A trial_local account has none of these rows, so this does
not block the trial-account dogfood use case, but it IS a real gap for paid
Bedrock-managed, ChatGPT-OAuth, and BYOK accounts — those credentials
survive a wipe today. Follow-up work, not yet built.

KNOWN GAP — DDB-orphaned Paperclip state: the Better Auth user delete
resolves its target from the DDB identity row, so a Better Auth user whose
identity row is missing survives the wipe. It is now reported loudly rather than
skipped silently (``_NO_PAPERCLIP_ROW``) — see that constant
for why enumerating from Aurora instead is not a safe fix.

Also NOT wiped: the ``company-slugs`` table (a wiped account keeps its slug
reservation permanently — there is no delete path, so the name can never be
handed to another owner) and the ``agent-email-addresses`` table (once
``AGENT_EMAIL_DOMAIN`` is live, a wiped account leaves its address row
pointing at a deleted owner, so inbound mail to that address has nowhere
valid to route). No collaborator exists yet for either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal

import httpx

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.billing.metronome_client import for_settings as metronome_for_settings
from core.config import settings
from core.services.hermes_provisioning import delete_workspace_runtime
from core.services.voice_calls import wipe_owner_voice
from core.repositories import (
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    messaging_repo,
    provisioning_workflow_repo,
    slack_installs_repo,
    usage_repo,
    user_repo,
)
from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipRepo
from core.services import clerk_admin, company_stripe, message_search, text_lines, voice_onboarding
from core.services.billing_service import delete_stripe_customer
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.oauth_service import revoke_user_oauth
from core.services.owner_secret_sweep import (
    delete_owner_agent_key_secrets,
    delete_owner_secrets,
    list_owner_agent_key_secret_names,
    list_owner_secret_names,
)
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

logger = logging.getLogger(__name__)


async def release_text_lines(rows: list[dict]) -> tuple[int, list[str]]:
    """Release the texting line of every owner holding a ``text`` or ``text_pending`` row, through the one
    release path (``text_lines.release_line``: cancel a pending operation, release a finished one or the line).

    Best effort. Returns (owners released, what failed to release: an operation or a line)."""
    by_owner: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("kind") in ("text", "text_pending"):
            by_owner.setdefault(r.get("owner_id", ""), []).append(r)
    released, failed = 0, []
    for owner_id, owned in by_owner.items():
        try:
            await text_lines.release_line(owner_id)
            released += 1
        except Exception as exc:
            logger.warning("wipe: could not release the text line of %s: %s", owner_id, exc)
            failed += [
                f"operation {r['operation_id']}" if r.get("kind") == "text_pending" else r.get("address") or "?"
                for r in owned
            ]
    return released, failed


def text_release_note(released: int, failed: list[str]) -> str:
    note = f", released {released} text line(s)"
    if failed:
        note += f", FAILED to release: {', '.join(failed)} (rows kept; run the wipe again)"
    return note


# "refused" = a guard kept the resource on purpose (a shared workspace company).
# Neither a success nor a failure, and never a delete.
WipeStatus = Literal["ok", "skipped", "failed", "would_delete", "refused"]

# The Better Auth user delete resolves its target from the DDB identity row and
# NOTHING else, so a Better Auth user whose identity row never landed (partial
# provision, prior partial wipe, duplicate-signup debris) is left behind. That
# is not fixable by "enumerate from Aurora instead": Aurora carries no Isol8
# owner id, and an email-keyed lookup cannot tell debris from a LIVE account.
# Until Paperclip carries an owner-id attribution column, the honest move is to
# say so in the report instead of reporting a clean skip.
_NO_COMPANY = "no workspace membership — a human's wipe never deletes a Paperclip company"

_NO_PAPERCLIP_ROW = (
    "no paperclip identity row — Aurora may still hold a Better Auth user for this owner "
    "(partial provision / prior partial wipe). Not resolvable from the owner id; check "
    "Aurora by the owner's email before treating the Paperclip side as gone."
)


class BootstrapAccountProtectedError(Exception):
    """Raised up front (before any teardown step) by preview_wipe/wipe_account when
    the target account's email is a bootstrap Paperclip admin identity
    (admin@isol8.co / admin+paperclip@isol8.co). Deleting either breaks the
    PaperclipAdminClient bootstrap-admin session every provision depends on. The
    router turns this into a 400."""

    def __init__(self, owner_id: str):
        super().__init__(f"Account wipe refuses bootstrap Paperclip admin account {owner_id!r}.")
        self.owner_id = owner_id


async def resolve_account_emails(owner_id: str) -> list[str]:
    """Every email address known for the owner, Clerk first. Clerk is the source of
    truth for the type-to-confirm match and the bootstrap refusal: the users DDB
    row's email is written by an unreliable webhook and is frequently absent, so
    matching against it alone can make a legitimate wipe impossible to confirm.
    Falls back to the users-row email (deduped) when Clerk is unavailable
    (e.g. CLERK_SECRET_KEY unset in local/test)."""
    emails: list[str] = []
    clerk_user = await clerk_admin.get_user(owner_id)
    if clerk_user:
        for e in clerk_user.get("email_addresses") or []:
            addr = e.get("email_address")
            if addr and addr not in emails:
                emails.append(addr)
    row = await user_repo.get(owner_id)
    row_email = (row or {}).get("email")
    if row_email and row_email not in emails:
        emails.append(row_email)
    return emails


def _refuse_bootstrap_account(owner_id: str, emails: list[str]) -> None:
    lowered = {e.lower() for e in emails}
    if lowered & {b.lower() for b in BOOTSTRAP_EMAILS}:
        raise BootstrapAccountProtectedError(owner_id)


class OrgWipeUnsupportedError(Exception):
    """Raised by preview_wipe/wipe_account when ``owner_id`` names a Clerk
    organization.

    The wipe targets one Clerk user — org accounts have per-member fan-out
    (N Paperclip rows + N Better Auth users, shared company), which needs a
    looping per-member teardown that is a documented follow-up (design
    spec's "Out of scope: Org accounts"). A user who is a MEMBER of an org is
    accepted; no company is ever deleted by a human's wipe. The
    router (Task 4) turns this into a 400.
    """

    def __init__(self, owner_id: str):
        super().__init__(
            f"Account wipe does not support org owners (v1 is personal-only): "
            f"{owner_id!r} is a Clerk organization id, not a user id."
        )
        self.owner_id = owner_id


@dataclass
class WipeStep:
    system: str
    action: str
    status: WipeStatus
    detail: str = ""
    error: str | None = None


@dataclass
class WipeReport:
    owner_id: str
    steps: list[WipeStep]
    dry_run: bool
    workspaces: list[dict] = field(default_factory=list)
    """``_workspace_memberships``: every workspace the human belongs to (whose
    companies the wipe keeps). The UI asks for the first one's name before
    executing.
    (the UI asks for the workspace name before executing), else None."""

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "dry_run": self.dry_run,
            "workspaces": self.workspaces,
            "steps": [
                {
                    "system": s.system,
                    "action": s.action,
                    "status": s.status,
                    "detail": s.detail,
                    "error": s.error,
                }
                for s in self.steps
            ],
        }


@dataclass
class _Identifiers:
    """The identifier rows read up front, before any delete runs — steps
    6-11 (bedrock/metronome/stripe/DDB cleanup) all need values off these
    rows, and some of those rows get deleted by earlier steps.

    WHAT THIS SNAPSHOT IS NOT: authoritative about what exists. Every read
    below is eventually consistent except ``get_pending_brief``, and all of
    them are taken before the first teardown step runs — so a row written by a
    provision (or an onboarding submission) that overlaps the wipe is missing
    from it. Use it to CARRY values a later step can no longer read (ARNs off a
    row an earlier step deletes) and to shape the preview. Never let it decide
    that an idempotent delete can be skipped: that is how a wipe reports
    success over a row it never touched. Two steps take the fresh answer
    instead — ``bedrock_gate_tokens`` (re-reads consistently, deletes the
    union) and ``paperclip_row`` (always attempts the pending-brief delete).

    The Bedrock footprint no longer reads this snapshot at all: it is reaped
    inside the ``runtime`` step off the containers row's own ALL_OLD image,
    so an AIP minted mid-wipe is caught as long as the row named it when it
    died. Still snapshot-gated, and knowingly: the ``runtime`` step itself,
    and the ``billing_row`` / ``user_row`` / paperclip-company deletes.
    None of those rows is the sole handle on anything else, so the residue is
    findable and a re-run clears it.
    """

    user_row: dict | None
    container: dict | None
    paperclip: PaperclipIdentity | None
    """The human's identity row (their Better Auth account)."""
    workspaces: list[str]
    """Every workspace (Clerk org id) the human belongs to — read strict, so a
    Clerk blip stops the wipe before its first step."""
    billing: dict | None
    pending_brief: dict | None
    """The owner's ``__pending_brief__:`` item, if any. Read independently of
    ``paperclip`` because it is a SEPARATE item in the same table under a
    prefixed key, and routinely outlives (or predates) the company row — the
    funnel is answered during provisioning, so an owner whose seed never landed
    has a pending brief and no company row at all."""


async def _read_identifiers(owner_id: str) -> _Identifiers:
    user_row = await user_repo.get(owner_id)
    container = await container_repo.get_by_owner_id(owner_id)
    paperclip_repo = PaperclipRepo(table_name="paperclip-companies")
    paperclip = await paperclip_repo.get_identity(owner_id)
    pending_brief = await paperclip_repo.get_pending_brief(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    workspaces = await _workspaces_of(owner_id)
    return _Identifiers(
        user_row=user_row,
        container=container,
        paperclip=paperclip,
        workspaces=workspaces,
        billing=billing,
        pending_brief=pending_brief,
    )


def _refuse_org_owners(owner_id: str) -> None:
    if is_org_owner_id(owner_id):
        raise OrgWipeUnsupportedError(owner_id)


async def _workspaces_of(user_id: str) -> list[str]:
    """The human's workspaces from Clerk. Strict: a read failure raises, and
    the wipe stops before its first step instead of guessing "none"."""
    orgs = await clerk_admin.list_user_organizations(user_id, strict=True)
    return sorted({o["id"] for o in orgs if o.get("id") and is_org_owner_id(o["id"])})


def _org_hint_for_missing_container(workspaces: list[str]) -> str:
    """Loud skip detail when a user-keyed wipe finds no containers row.

    Incident 2026-09-21: a user-keyed wipe of a workspace member misses the
    containers row (it lives under the ORG id) while every other user-keyed
    row is still destroyed — a half-wipe that reads as a clean skip. When the
    human belongs to a workspace, say so and point at the workspace wipe
    (``core.services.workspace_wipe``) instead of reporting a bare absence.
    Never deletes anything; detail text only.
    """
    if workspaces:
        org_id = workspaces[0]
        return (
            "no containers row under this USER id — but this account belongs to "
            f"workspace {org_id}, whose runtime lives under the ORG id. A user-keyed "
            "wipe cannot reach it; use the workspace wipe "
            f"(POST /admin/workspaces/{org_id}/wipe) instead."
        )
    return "no containers row"


#: What removing a workspace's LAST member sets off: Clerk keeps the empty org, and the
#: ``organizationMembership.deleted`` webhook's last-member teardown (#3115,
#: ``routers/webhooks._teardown_if_no_members_left``) runs the org-deletion path. The wipe
#: itself deletes none of this -- which is exactly why the preview has to say so.
LAST_MEMBER_TEARDOWN = (
    "cancels the workspace's Stripe subscription",
    "deletes the workspace's agent runtime",
    "disables its Teams workspace, which stops every routine including the nightly CEO run",
    "retires its managed AI key and revokes its join link",
)


async def _workspace_memberships(workspaces: list[str], owner_id: str) -> list[dict]:
    """The facts of EVERY workspace the human belongs to, for the report and
    the admin's workspace-name confirm. Each workspace's company is kept: a
    human's wipe never deletes a company -- but when ``owner_id`` is a workspace's
    LAST member, removing them tears that workspace down (``LAST_MEMBER_TEARDOWN``).

    "Last" is Clerk's roster, read STRICT (the same read that decides the teardown):
    a Clerk blip raises and stops the preview or wipe, rather than hiding a teardown."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    out: list[dict] = []
    for org_id in workspaces:
        ws = await repo.get_workspace(org_id)
        provisioner = ws.provisioner_user_id if ws is not None else ""
        identity = await repo.get_identity(provisioner) if provisioner else None
        org = await clerk_admin.get_organization(org_id)
        # No Clerk key (tests, local dev): the webhook cannot read a roster either, so it tears
        # nothing down -- unknown is not "empty", on both sides.
        roster = await clerk_admin.list_organization_members(org_id, strict=True) if settings.CLERK_SECRET_KEY else None
        last_member = roster is not None and all(m.get("user_id") == owner_id for m in roster)
        out.append(
            {
                "company_id": ws.company_id if ws is not None else "",
                "org_id": org_id,
                "workspace_name": (org or {}).get("name") or None,
                "owner_user_id": provisioner or None,
                "owner_email": (identity.paperclip_email or None) if identity is not None else None,
                "member_count": len(ws.member_user_ids) if ws is not None else 0,
                "last_member": last_member,
                "teardown": list(LAST_MEMBER_TEARDOWN) if last_member else [],
            }
        )
    return out


async def resolve_workspace_memberships(owner_id: str) -> list[dict]:
    """``_workspace_memberships`` for ``owner_id`` — the admin router's input
    for its workspace-name confirm."""
    return await _workspace_memberships(await _workspaces_of(owner_id), owner_id)


def _refused_detail(memberships: list[dict]) -> str:
    parts = []
    for m in memberships:
        owner = m["owner_user_id"] or "unknown"
        if m["owner_email"]:
            owner += f" <{m['owner_email']}>"
        name = f'workspace "{m["workspace_name"]}"' if m["workspace_name"] else "a workspace"
        parts.append(
            f"company {m['company_id'] or '-'} of {name} ({m['org_id']}, owner {owner}, {m['member_count']} member(s))"
        )
    return "refused: a human's wipe never deletes a company — kept: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Compound collaborators — wrapped so each is a single mockable async unit.
# ---------------------------------------------------------------------------
#
# Both PaperclipAdminClient.delete_company and MetronomeClient.archive_customer
# need an httpx.AsyncClient the caller owns the lifecycle of (mirrors
# metronome_gate.read_net_balance_microcents elsewhere in this codebase). Wrapping the open/call/close sequence here
# keeps the orchestrator's own step list to one call per collaborator.


async def _delete_paperclip_company_call(company_id: str) -> None:
    """PaperclipAdminClient.delete_company, authenticated as the bootstrap
    admin. 404 (already gone) is swallowed by delete_company itself."""
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)
    try:
        admin = PaperclipAdminClient(http_client=http)
        cookie = await get_admin_session_cookie(http)
        await admin.delete_company(session_cookie=cookie, company_id=company_id)
    finally:
        await http.aclose()


async def _archive_paperclip_member_call(user_id: str, org_id: str) -> None:
    """The same removal ``organizationMembership.deleted`` uses: drops only this member from
    THIS workspace's members (``remove_member``, a no-op once the row is gone)."""
    await PaperclipRepo(table_name="paperclip-companies").remove_member(org_id, user_id)


async def _archive_metronome_customer(customer_id: str) -> None:
    """MetronomeClient.archive_customer via the settings-derived client
    (mirrors metronome_gate.read_net_balance_microcents's client lifecycle)."""
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
        await metronome_for_settings(http).archive_customer(customer_id=customer_id)


# ---------------------------------------------------------------------------
# Step-runner helpers
# ---------------------------------------------------------------------------


def _runtime_detail(report: dict) -> str:
    """The runtime step's detail; "scheduled" when a parked member keeps the runtime a while."""
    if report.get("runtime") == "scheduled":
        return (
            "containers row + channel-links removed; agentcore runtime + boot secret + S3 profiles "
            "teardown scheduled (after the cloud member forgets its entries, within a few minutes)"
        )
    return "agentcore runtime + boot secret + S3 profiles + containers row + channel-links removed"


async def _step(steps: list[WipeStep], *, system: str, action: str, fn: Callable[[], Awaitable[str | None]]) -> None:
    """Run one teardown collaborator, in its own try/except.

    ``fn`` performs the delete and returns a short detail string (or None).
    A raise is recorded as a "failed" step with the error message and does
    NOT propagate — every other step still runs. This is the partial-failure
    tolerance the design spec calls out as the orchestrator's core property.
    """
    try:
        detail = await fn()
        steps.append(WipeStep(system=system, action=action, status="ok", detail=detail or ""))
    except Exception as e:  # noqa: BLE001 - partial-failure tolerance is the point
        logger.exception("account_wipe: step failed system=%s action=%s", system, action)
        steps.append(WipeStep(system=system, action=action, status="failed", error=str(e)))


def _skip(steps: list[WipeStep], system: str, action: str, detail: str) -> None:
    steps.append(WipeStep(system=system, action=action, status="skipped", detail=detail))


async def cancel_open_workflows(owner_id: str) -> tuple[int, int]:
    """Cancel every non-terminal provisioning workflow — never deprovision.

    The row is OUR durable memory of an ask, and closing it makes it terminal,
    so no later read of it touches Stripe for this owner again. The provider
    resources themselves are the owner's and are left standing (spec §3.6);
    this is a pure DDB write and reaches no provider. Shared by the account
    wipe and the owner's revoke-all. Returns (cancelled, open_count) — a
    transition returning None means someone else moved the row first, which is
    closed-or-moving either way and not an error here.
    """
    rows = await provisioning_workflow_repo.list_for_owner(owner_id)
    open_rows = [r for r in rows if r.get("state") not in provisioning_workflow_repo.TERMINAL_STATES]
    cancelled = 0
    for row in open_rows:
        if await provisioning_workflow_repo.transition(
            row["workflow_id"], from_state=row["state"], to_state="cancelled"
        ):
            cancelled += 1
    return cancelled, len(open_rows)


async def _wipe_company_stripe(steps: list[WipeStep], *, owner_id: str) -> None:
    """Step 8b: revoke the platform's link to the company's Stripe account.

    NEVER deletes the account — it is the owner's business account and keeps
    working as a normal Stripe account after we let go (spec §3.6).

    Reads the billing row FRESH (strongly consistent) rather than taking the
    ``_read_identifiers`` snapshot: everything above this step is slow (ECS,
    EFS, Aurora), and an OAuth callback landing in that window binds an
    account the snapshot cannot know about. Deauthorizing a stale id — or
    skipping a live one and leaving the platform attached to the owner's
    Stripe account after their account is gone — is the failure this avoids.
    """
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        _skip(steps, "stripe", "deauthorize_company_account", "no company_stripe_account_id on billing row")
        return

    async def _s8b(account_id: str = account_id) -> str:
        # `release_and_unbind` clears the binding whatever Stripe answers —
        # the unbind used to sit on the far side of the deauthorize, so a
        # raise left the row bound and the platform attached, which is the
        # single failure this step exists to prevent. Raising AFTER that
        # unbind is how the report records "Stripe never confirmed": _step
        # marks the step failed and the wipe carries on.
        if not await company_stripe.release_and_unbind(
            owner_id=owner_id, account_id=account_id, source=row.get("company_stripe_source")
        ):
            raise RuntimeError(f"unbound {account_id} locally, but Stripe did not confirm the revoke")
        return f"company_stripe_account_id={account_id} deauthorized (account retained by owner)"

    await _step(steps, system="stripe", action="deauthorize_company_account", fn=_s8b)


def _preview_step(
    steps: list[WipeStep],
    *,
    system: str,
    action: str,
    exists: bool,
    detail_if_exists: str,
    detail_if_absent: str,
) -> None:
    steps.append(
        WipeStep(
            system=system,
            action=action,
            status="would_delete" if exists else "skipped",
            detail=detail_if_exists if exists else detail_if_absent,
        )
    )


# ---------------------------------------------------------------------------
# preview_wipe — read-only
# ---------------------------------------------------------------------------


async def preview_wipe(owner_id: str) -> WipeReport:
    """Read-only enumeration of what a wipe would delete. Calls NO delete
    method — every read here is a GetItem/Query (or, for the EFS workspace
    directory, a filesystem existence check)."""
    _refuse_org_owners(owner_id)
    _refuse_bootstrap_account(owner_id, await resolve_account_emails(owner_id))

    ids = await _read_identifiers(owner_id)
    memberships = await _workspace_memberships(ids.workspaces, owner_id)
    steps: list[WipeStep] = []

    if memberships:
        steps.append(WipeStep("paperclip_company", "delete_company", "refused", _refused_detail(memberships)))
        steps.append(
            WipeStep(
                "paperclip_membership",
                "archive_member",
                "would_delete",
                f"only this member's membership is archived in {', '.join(ids.workspaces)} "
                "(Paperclip reassigns their issues); every company is kept",
            )
        )
    else:
        _skip(steps, "paperclip_company", "delete_company", _NO_COMPANY)
    _preview_step(
        steps,
        system="paperclip_aurora_user",
        action="delete_paperclip_user",
        exists=ids.paperclip is not None,
        detail_if_exists=(f"paperclip_user_id={ids.paperclip.paperclip_user_id}" if ids.paperclip else ""),
        detail_if_absent=_NO_PAPERCLIP_ROW,
    )
    gate_token_hashes = owner_gate_token_hashes(ids.container)
    _preview_step(
        steps,
        system="bedrock_gate_tokens",
        action="delete_gate_token_rows",
        exists=bool(gate_token_hashes),
        detail_if_exists=f"{len(gate_token_hashes)} gate-token row(s)",
        detail_if_absent="no bedrock-gate token hash on containers row",
    )
    _preview_step(
        steps,
        system="runtime",
        action="delete_workspace_runtime",
        exists=ids.container is not None,
        detail_if_exists=(
            f"agentcore_runtime_arn={(ids.container or {}).get('agentcore_runtime_arn', '')} "
            "(+ boot secret, S3 profiles, containers row, channel-links, "
            "and the Bedrock footprint: gate-token rows, AIPs, IAM role)"
        ),
        detail_if_absent=_org_hint_for_missing_container(ids.workspaces),
    )

    # No standalone bedrock_aips / bedrock_role preview steps: the executed wipe
    # no longer has them either. Both are reaped inside the runtime step, by
    # the shared reaper at the seam every teardown path crosses. Preview and
    # execution have to list the same systems or the preview lies.

    metronome_customer_id = (ids.billing or {}).get("metronome_customer_id")
    _preview_step(
        steps,
        system="metronome",
        action="archive_customer",
        exists=bool(metronome_customer_id),
        detail_if_exists=metronome_customer_id or "",
        detail_if_absent="no metronome_customer_id on billing row",
    )

    stripe_customer_id = (ids.billing or {}).get("stripe_customer_id")
    _preview_step(
        steps,
        system="stripe",
        action="delete_stripe_customer",
        exists=bool(stripe_customer_id),
        detail_if_exists=stripe_customer_id or "",
        detail_if_absent="no stripe_customer_id on billing row",
    )

    company_stripe_account_id = (ids.billing or {}).get("company_stripe_account_id")
    _preview_step(
        steps,
        system="stripe",
        action="deauthorize_company_account",
        exists=bool(company_stripe_account_id),
        detail_if_exists=company_stripe_account_id or "",
        detail_if_absent="no company_stripe_account_id on billing row",
    )

    # usage-counters has no cheap existence check (would need a bounded
    # Query); delete_all_for_owner is idempotent on an empty set, so the
    # preview always shows would_delete rather than adding a second read.
    steps.append(
        WipeStep(
            system="usage_counters",
            action="delete_all_for_owner",
            status="would_delete",
            detail="all usage-counter rows for owner",
        )
    )
    room_ids = await messaging_repo.list_owner_room_ids(owner_id)
    steps.append(
        WipeStep(
            system="messaging",
            action="delete_owner_rooms_and_streams",
            status="would_delete",
            detail=(f"{len(room_ids)} owned room partition(s), then owner catalog and private user-stream mappings"),
        )
    )
    steps.append(
        WipeStep(
            system="search",
            action="drop_owner_index",
            status="would_delete",
            detail="the owner's message search index",
        )
    )
    steps.append(
        WipeStep(
            system="person_memory",
            action="delete_person_memory",
            status="would_delete",
            detail="uploaded sessions, drafts, episodes and computer keys",
        )
    )

    paperclip_rows = [
        label
        for label, present in (
            ("paperclip identity row", ids.paperclip is not None),
            ("pending_brief row", ids.pending_brief is not None),
        )
        if present
    ]
    _preview_step(
        steps,
        system="paperclip_row",
        action="paperclip_repo.delete",
        exists=bool(paperclip_rows),
        detail_if_exists=" + ".join(paperclip_rows),
        detail_if_absent="no paperclip-companies or pending_brief row",
    )
    _preview_step(
        steps,
        system="billing_row",
        action="billing_repo.delete",
        exists=ids.billing is not None,
        detail_if_exists="billing-accounts row",
        detail_if_absent="no billing-accounts row",
    )
    _preview_step(
        steps,
        system="user_row",
        action="user_repo.delete",
        exists=ids.user_row is not None,
        detail_if_exists="users row",
        detail_if_absent="no users row",
    )

    # Per-agent Paperclip key secrets — no per-agent index exists, so list
    # them by SM name prefix (read-only) to show an accurate count.
    agent_key_names = await list_owner_agent_key_secret_names(owner_id)
    _preview_step(
        steps,
        system="paperclip_agent_keys",
        action="delete_owner_agent_key_secrets",
        exists=bool(agent_key_names),
        detail_if_exists=f"{len(agent_key_names)} per-agent Paperclip key secret(s)",
        detail_if_absent="no per-agent Paperclip key secrets",
    )

    # Clerk is external; we don't spend a live GET here just for the
    # preview (preview_wipe touches only our own DDB rows + EFS). Always
    # shown as would_delete — the same convention as usage_counters above.
    # Credential + identity leftovers. A preview that omits these tells an
    # admin the credentials survive — the impression this report exists to
    # correct. All read-only.
    open_workflows = [
        r
        for r in await provisioning_workflow_repo.list_for_owner(owner_id)
        if r.get("state") not in provisioning_workflow_repo.TERMINAL_STATES
    ]
    _preview_step(
        steps,
        system="provisioning",
        action="cancel_open_workflows",
        exists=bool(open_workflows),
        detail_if_exists=f"{len(open_workflows)} open provisioning workflow(s) would be cancelled (never deprovisioned)",
        detail_if_absent="no open provisioning workflows",
    )

    secret_names = await list_owner_secret_names(owner_id)
    _preview_step(
        steps,
        system="owner_secrets",
        action="delete_owner_secrets",
        exists=bool(secret_names),
        detail_if_exists=f"{len(secret_names)} secret(s): BYOK keys, integration creds, OAuth apps",
        detail_if_absent="no owner secrets",
    )

    integration_rows = await integrations_repo.list_for_owner(owner_id)
    _preview_step(
        steps,
        system="integrations",
        action="delete_all_for_owner",
        exists=bool(integration_rows),
        detail_if_exists=f"{len(integration_rows)} integration row(s)",
        detail_if_absent="no integration rows",
    )

    steps.append(WipeStep(system="oauth_tokens", action="revoke_user_oauth", status="would_delete", detail=owner_id))

    email_rows = await agent_email_repo.query_by_owner(owner_id)
    _preview_step(
        steps,
        system="agent_email_addresses",
        action="delete_all_for_owner",
        exists=bool(email_rows),
        detail_if_exists=f"{len(email_rows)} address(es): " + ", ".join(r.get("address", "?") for r in email_rows),
        detail_if_absent="no agent email addresses",
    )

    install_rows = await slack_installs_repo.list_for_owner(owner_id)
    steps.append(
        WipeStep(
            system="slack_installs",
            action="delete_all_for_owner",
            status="would_delete",
            detail=", ".join(r["team_id"] for r in install_rows) or "none",
        )
    )

    slug_row = await company_slug_repo.get_by_owner(owner_id)
    _preview_step(
        steps,
        system="company_slug",
        action="delete_for_owner",
        exists=slug_row is not None,
        detail_if_exists=f"slug {(slug_row or {}).get('slug')}",
        detail_if_absent="no slug reservation",
    )

    steps.append(WipeStep(system="clerk_user", action="delete_user", status="would_delete", detail=owner_id))

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=True, workspaces=memberships)


# ---------------------------------------------------------------------------
# wipe_account — the ordered teardown
# ---------------------------------------------------------------------------


async def wipe_account(owner_id: str, *, actor_admin_id: str, account_emails: list[str] | None = None) -> WipeReport:
    """Execute the ordered teardown (design spec steps 1-12).

    ``actor_admin_id`` is not used for a delete decision here — it exists so
    callers (and any future audit hook inside this function) have the actor
    without threading it through every step. The router (Task 4) is the one
    that writes the admin-actions audit row with this id + the returned
    report.

    ``account_emails`` may be pre-resolved by the caller (the router already
    resolves them for the type-to-confirm match) to avoid a second Clerk call;
    when None we resolve them here. Either way the bootstrap-admin refusal
    fires up front, before any teardown step, off the Clerk-authoritative list.
    """
    _refuse_org_owners(owner_id)

    emails = account_emails if account_emails is not None else await resolve_account_emails(owner_id)
    _refuse_bootstrap_account(owner_id, emails)

    ids = await _read_identifiers(owner_id)
    # Before any step runs: a raise here stops the wipe with nothing deleted.
    memberships = await _workspace_memberships(ids.workspaces, owner_id)
    steps: list[WipeStep] = []

    email = emails[0] if emails else ""
    env = settings.ENVIRONMENT or "dev"

    logger.info(
        "account_wipe.wipe_account: starting for owner=%s actor=%s",
        owner_id,
        actor_admin_id,
    )

    # --- Step 1 (spec #1): Paperclip company ------------------------------
    # A company is its workspace's: a human's wipe never deletes one. Remove
    # only this human from each workspace's company (the Better Auth user
    # delete in step 2 is their own identity; its FKs only cascade sessions).
    if memberships:
        logger.warning(
            "account_wipe: owner=%s belongs to %s — delete_company REFUSED, archiving membership",
            owner_id,
            ids.workspaces,
        )
        steps.append(WipeStep("paperclip_company", "delete_company", "refused", _refused_detail(memberships)))

        async def _s1m() -> str:
            for org_id in ids.workspaces:
                await _archive_paperclip_member_call(owner_id, org_id)
            return f"membership archived in {', '.join(ids.workspaces)}; companies kept"

        await _step(steps, system="paperclip_membership", action="archive_member", fn=_s1m)
    else:
        _skip(steps, "paperclip_company", "delete_company", _NO_COMPANY)

    # --- Step 2 (spec #2): Paperclip Aurora (Better Auth) user ------------
    # BootstrapUserProtectedError (the two bootstrap admin emails) is caught
    # by _step's generic except like any other failure -- it must not abort
    # the rest of the wipe, per the design spec's partial-failure model.
    if ids.paperclip is not None:
        paperclip_user_id = ids.paperclip.paperclip_user_id

        async def _s2(paperclip_user_id: str = paperclip_user_id) -> str:
            await delete_paperclip_user(paperclip_user_id=paperclip_user_id, email=email, env=env)
            return f"paperclip_user_id={paperclip_user_id}"

        await _step(steps, system="paperclip_aurora_user", action="delete_paperclip_user", fn=_s2)
    else:
        _skip(steps, "paperclip_aurora_user", "delete_paperclip_user", _NO_PAPERCLIP_ROW)

    # --- Step 2b: bedrock-gate token rows ----------------------------------
    # The gate-tokens table is keyed on the token hash with NO owner index, so
    # the containers row — ``bedrock_gate_token_hash`` plus every hash tracked
    # in ``bedrock_gate_superseded_hashes`` — is the only handle on an owner's
    # rows. Step 3 below destroys that row (inside delete_user_service), so
    # this is ordered ahead of it: a row that outlives its handle is
    # unreachable forever — unfindable, unexpirable, and outside
    # deny_gate_token's reach, at ``denied=False``. Four were stranded that way
    # in prod by wipes run before this step existed.
    #
    # THE SNAPSHOT IS NOT AUTHORITATIVE HERE, and ordering alone does not make
    # it so. ``_read_identifiers`` reads the containers row eventually
    # consistently, and reads it before the Paperclip steps above run — so a
    # ``mint_gate_token`` that landed just before the wipe, or concurrently with
    # those steps, can leave its ``denied=False`` hash off ``ids.container``
    # entirely. Re-read STRONGLY CONSISTENT at the moment of use and delete the
    # UNION of both: deleting a hash twice is a free no-op, missing one is
    # permanent. (Union, not replacement — a mint that superseded and pruned a
    # hash between the two reads leaves it named only by the snapshot.)
    #
    # The window this cannot close — a mint between that read and the row's
    # death — is closed by the sweep in step 3, off the DELETE's own ALL_OLD
    # image. That sweep also backstops this step failing outright.
    snapshot_gate_hashes = owner_gate_token_hashes(ids.container)

    async def _s2b() -> str:
        fresh = await container_repo.get_by_owner_id(owner_id, consistent=True)
        hashes = list(dict.fromkeys([*snapshot_gate_hashes, *owner_gate_token_hashes(fresh)]))
        if not hashes:
            # chatgpt_oauth / byo_key owners never mint one — a genuine no-op.
            return "no bedrock-gate token hash on containers row"
        deleted = await delete_gate_token_rows(hashes)
        return f"deleted {deleted} of {len(hashes)} gate-token row(s)"

    await _step(steps, system="bedrock_gate_tokens", action="delete_gate_token_rows", fn=_s2b)

    # --- Step 3 (spec #3 + #4 + #5 folded): the runtime ------------------
    # ``hermes_provisioning.delete_workspace_runtime`` already does, in the right internal
    # order: StopRuntimeSession + DeleteAgentRuntime -> revoke the live gate-token hash ->
    # delete the boot secret -> delete every object under ``<owner>/hermes/`` (spec #4's
    # "content" step, which on this transport IS the S3 profile prefix -- there is no EFS
    # tree or access point left) -> delete the containers row (part of spec #11) -> reap the
    # owner's Bedrock footprint -> sweep channel-links rows. One WipeStep stands in for spec
    # rows #3, #4, #5 and the containers-row/channel-links slice of #11.
    if ids.container is not None:

        async def _s3() -> str:
            try:  # end live voice calls before the ledger is archived; never abort the wipe
                await wipe_owner_voice(owner_id)
            except Exception:  # noqa: BLE001
                logger.warning("voice wipe failed owner=%s", owner_id, exc_info=True)
            # M6-R42: this is one of exactly THREE terminal callers that pass
            # ``delete_profiles=True`` -- this wipe, ``workspace_wipe`` (org-keyed),
            # and ``routers/debug.py``'s ``DELETE /debug/user-data`` (the e2e
            # empty-slate reset; debug is 403 in prod).
            # Every other caller (entitlement_teardown on a lapsed card, debug's routine
            # reprovision) leaves it False and keeps the owner's memory/skills/SOUL.md/
            # auth.json. Do not re-broaden it.
            report: dict = {}
            row_at_death = await delete_workspace_runtime(owner_id, delete_profiles=True, report=report)
            detail = _runtime_detail(report)
            # FINAL SWEEP — the last moment any gate-token hash is reachable.
            # ``delete_workspace_runtime`` returns the row's own ALL_OLD image, so
            # this covers exactly what step 2b could not see: a hash minted
            # after its strongly-consistent re-read, and every hash at all if
            # that step failed. Already-deleted hashes cost a free no-op
            # DeleteItem and are reported as 0, so no bookkeeping is needed to
            # tell the two apart. A failure here fails THIS step rather than
            # passing silently — the residue is real and the wipe is retryable.
            late_hashes = owner_gate_token_hashes(row_at_death)
            swept = await delete_gate_token_rows(late_hashes) if late_hashes else 0
            if swept:
                detail += f" (+ swept {swept} gate-token row(s) still named on the row at delete time)"
            return detail

        await _step(steps, system="runtime", action="delete_workspace_runtime", fn=_s3)
    else:
        _skip(steps, "runtime", "delete_workspace_runtime", _org_hint_for_missing_container(ids.workspaces))

    # --- Steps 5-6 (spec #6-7-8): Bedrock AIPs + owner role ----------------
    # Deliberately NOT separate steps any more. Both are reaped inside step 3's
    # ``delete_workspace_runtime`` (see bedrock_aip.reap_owner_bedrock_resources),
    # which is the seam EVERY teardown path crosses -- the entitlement
    # cancellation webhook and both /debug teardowns leaked these until it moved
    # there, and dev/staging/prod share one AWS account and therefore one
    # 1,000-role IAM quota.
    #
    # This is an upgrade, not just deduplication. These steps reaped from
    # ``ids.container``, the snapshot taken at the TOP of the wipe; the seam
    # reaps from the row's own ALL_OLD image at the instant it was deleted,
    # which is the authoritative answer to what the row named when it died.
    # Step 6 was also gated on ``bedrock_role_arn`` being present on that
    # snapshot, so a legacy row without the field silently skipped the role
    # delete and stranded it forever -- the reaper derives the role name from
    # owner_id and never gates on the field.

    # --- Step 7 (spec #9): Metronome ---------------------------------------
    metronome_customer_id = (ids.billing or {}).get("metronome_customer_id")
    if metronome_customer_id:

        async def _s7(metronome_customer_id: str = metronome_customer_id) -> str:
            await _archive_metronome_customer(metronome_customer_id)
            return metronome_customer_id

        await _step(steps, system="metronome", action="archive_customer", fn=_s7)
    else:
        _skip(steps, "metronome", "archive_customer", "no metronome_customer_id on billing row")

    # --- Step 8 (spec #10): Stripe ------------------------------------------
    stripe_customer_id = (ids.billing or {}).get("stripe_customer_id")
    if stripe_customer_id:

        async def _s8(stripe_customer_id: str = stripe_customer_id) -> str:
            deleted = await delete_stripe_customer(owner_id)
            return f"stripe_customer_id={stripe_customer_id} deleted={deleted}"

        await _step(steps, system="stripe", action="delete_stripe_customer", fn=_s8)
    else:
        _skip(steps, "stripe", "delete_stripe_customer", "no stripe_customer_id on billing row")

    # --- Step 8b: company Stripe account — deauthorize only ------------------
    await _wipe_company_stripe(steps, owner_id=owner_id)

    # --- Step 9 (spec #11, remaining tables) -------------------------------
    # containers + channel-links already removed inside step 3 above.

    async def _s9a() -> str:
        count = await usage_repo.delete_all_for_owner(owner_id)
        return f"deleted {count} row(s)"

    await _step(steps, system="usage_counters", action="delete_all_for_owner", fn=_s9a)

    async def _s9a2() -> str:
        room_ids = await messaging_repo.list_owner_room_ids(owner_id)
        deleted = 0
        # Keep the owner catalog until every room partition is gone. A partial
        # failure can then retry from the same complete room-id inventory.
        for room_id in room_ids:
            meta = await messaging_repo.get_room(room_id)
            if meta is not None and meta.get("ownerId") != owner_id:
                raise RuntimeError("messaging owner catalog referenced a room owned elsewhere")
            deleted += await messaging_repo.delete_room_partition(room_id)
        deleted += bool(await voice_onboarding.release_number(owner_id))  # the PHONE# line row, before the page goes
        deleted += await messaging_repo.delete_owner_partition(owner_id)
        return f"deleted {len(room_ids)} room partition(s) and {deleted} messaging row(s)"

    await _step(
        steps,
        system="messaging",
        action="delete_owner_rooms_and_streams",
        fn=_s9a2,
    )

    async def _s9a3() -> str:
        return await message_search.delete_owner_index(owner_id)

    await _step(steps, system="search", action="drop_owner_index", fn=_s9a3)

    async def _s9a4() -> str:
        # The person's own memory data, in no workspace's keeping: uploaded sessions, drafts and
        # evaluator rows, episodes, the computer-key registry (workspace_memory.delete_person_memory).
        from core.services import workspace_memory

        await workspace_memory.delete_person_memory(owner_id)
        return "sessions, drafts, episodes and computer keys removed"

    await _step(steps, system="person_memory", action="delete_person_memory", fn=_s9a4)

    # Both of this owner's items in paperclip-companies, not just the company
    # row: the funnel answer lives in the SAME table under the prefixed key
    # ``__pending_brief__:{owner_id}``, and it deliberately outlives a
    # reprovision (see PaperclipRepo's "LIFETIME" note). It is the user's own
    # typed words — "a social media agency for small restaurants" was one of
    # the two found stranded in prod — so a wipe that leaves it behind retains
    # content for an account we just deleted, in a row nothing will ever read
    # or clean up again (the key is the Clerk id, which is never reissued).
    # Folded into this step rather than given its own because it
    # is the same table, the same owner, and the same "delete the rows we
    # wrote" action — but it is NOT gated on the company row: the two exist
    # independently, and the prod case that surfaced this was a "SKIPPED — no
    # paperclip-companies row" wipe with a live pending_brief.
    #
    # Nor is it gated on the pending_brief snapshot. ``_read_identifiers`` runs
    # before every Paperclip step above, so an onboarding submission that lands
    # during the wipe leaves ``ids.pending_brief`` None while its item sits in
    # the table — the exact residue this exists to remove. The delete is
    # idempotent, so it is simply always attempted, and the step reports what
    # DynamoDB says it actually removed rather than what the snapshot predicted.
    async def _s9b() -> str:
        repo = PaperclipRepo(table_name="paperclip-companies")
        deleted: list[str] = []
        if ids.paperclip is not None:
            await repo.delete(owner_id)
            deleted.append("paperclip identity row deleted")
        if await repo.delete_pending_brief(owner_id):
            deleted.append("pending_brief row deleted")
        return " + ".join(deleted) or "no paperclip-companies or pending_brief row"

    await _step(steps, system="paperclip_row", action="paperclip_repo.delete", fn=_s9b)

    if ids.billing is not None:

        async def _s9c() -> str:
            await billing_repo.delete(owner_id)
            return "billing-accounts row deleted"

        await _step(steps, system="billing_row", action="billing_repo.delete", fn=_s9c)
    else:
        _skip(steps, "billing_row", "billing_repo.delete", "no billing-accounts row")

    if ids.user_row is not None:

        async def _s9d() -> str:
            await user_repo.delete(owner_id)
            return "users row deleted"

        await _step(steps, system="user_row", action="user_repo.delete", fn=_s9d)
    else:
        _skip(steps, "user_row", "user_repo.delete", "no users row")

    # --- Step 9e: per-agent Paperclip key secrets --------------------------
    # These live in Secrets Manager with no per-agent index, so the sweep enumerates
    # isol8/<env>/paperclip-agent-keys/<owner>/* by name prefix and deletes
    # each. Added after the 2026-07-14 account-wipe dogfood found them
    # orphaned post-wipe. Unconditional + idempotent (no-ops on an empty set).
    async def _s9e() -> str:
        count = await delete_owner_agent_key_secrets(owner_id)
        return f"deleted {count} per-agent key secret(s)"

    await _step(steps, system="paperclip_agent_keys", action="delete_owner_agent_key_secrets", fn=_s9e)

    # --- Credential + identity leftovers ----------------------------------
    # These were the module's documented KNOWN GAP: the owner's ChatGPT
    # tokens, BYOK LLM keys and integration credentials all survived a
    # "wipe", and their agent email addresses kept routing inbound SES mail
    # at an owner that no longer existed. Unconditional and idempotent —
    # each no-ops on an empty set.

    # Any provisioning workflow still in flight is CANCELLED — never
    # deprovisioned (see cancel_open_workflows' docstring). It
    # runs BEFORE the credential deletes below so a reader mid-step cannot land
    # a fresh secret after those have already run.
    async def _s9e2() -> str:
        cancelled, open_count = await cancel_open_workflows(owner_id)
        return f"cancelled {cancelled} of {open_count} open provisioning workflow(s)"

    await _step(steps, system="provisioning", action="cancel_open_workflows", fn=_s9e2)

    async def _s9f() -> str:
        names = await delete_owner_secrets(owner_id)
        return f"deleted {len(names)} owner secret(s) (BYOK keys, integration creds, OAuth apps)"

    await _step(steps, system="owner_secrets", action="delete_owner_secrets", fn=_s9f)

    async def _s9g() -> str:
        providers = await integrations_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(providers)} integration row(s): {', '.join(providers) or 'none'}"

    await _step(steps, system="integrations", action="delete_all_for_owner", fn=_s9g)

    # Deletes OUR stored tokens. It does NOT revoke the grant upstream at
    # OpenAI — there is no call to an OpenAI revocation endpoint anywhere in
    # this codebase, so the grant stays live on their side until it expires
    # or the human revokes it in their OpenAI account settings.
    async def _s9h() -> str:
        await revoke_user_oauth(user_id=owner_id)
        return "stored chatgpt oauth tokens deleted (grant NOT revoked upstream at OpenAI)"

    await _step(steps, system="oauth_tokens", action="revoke_user_oauth", fn=_s9h)

    async def _s9i() -> str:
        released, failed = await release_text_lines(await agent_email_repo.query_by_owner(owner_id))
        addresses = await agent_email_repo.delete_all_for_owner(owner_id)
        return (
            f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}"
            + text_release_note(released, failed)
        )

    await _step(steps, system="agent_email_addresses", action="delete_all_for_owner", fn=_s9i)

    async def _s9i2() -> str:
        teams = await slack_installs_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"

    await _step(steps, system="slack_installs", action="delete_all_for_owner", fn=_s9i2)

    async def _s9j() -> str:
        slug = await company_slug_repo.delete_for_owner(owner_id)
        return f"released slug {slug}" if slug else "no slug reservation"

    await _step(steps, system="company_slug", action="delete_for_owner", fn=_s9j)

    # --- Step 10 (spec #12): Clerk user -- ALWAYS LAST ---------------------
    # Deliberately unconditional and unguarded by any row read above:
    # leaving the Clerk user alive lets everything else regrow on the next
    # /container/status poll, so this must run regardless of what happened
    # to every step before it.
    async def _s10() -> str:
        await clerk_admin.delete_user(owner_id)
        return owner_id

    await _step(steps, system="clerk_user", action="delete_user", fn=_s10)

    logger.info(
        "account_wipe.wipe_account: finished for owner=%s actor=%s ok=%d skipped=%d failed=%d refused=%d",
        owner_id,
        actor_admin_id,
        sum(1 for s in steps if s.status == "ok"),
        sum(1 for s in steps if s.status == "skipped"),
        sum(1 for s in steps if s.status == "failed"),
        sum(1 for s in steps if s.status == "refused"),
    )

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=False, workspaces=memberships)
