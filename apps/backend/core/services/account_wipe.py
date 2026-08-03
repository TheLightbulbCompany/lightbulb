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
  front (so every later step already has the ARNs/ids it needs — no step
  re-reads a row another step might have just deleted), then runs each
  teardown step in its own try/except. A step that raises is recorded
  ``status="failed"`` with the error and execution CONTINUES — this
  partial-failure tolerance is the load-bearing property: a Paperclip
  timeout must not block the Clerk delete (step 12, always last, so a
  killed account can never regrow on the next ``/container/status`` poll).

Refuses org owners (``OrgWipeUnsupportedError``) — v1 targets personal
owners only (``owner_id == user_id``, one Paperclip company, one Better
Auth user). Org accounts have per-member fan-out; see the design spec's
"Out of scope: Org accounts".

Step-to-spec-row mapping. The design spec's teardown map lists 12 rows;
this orchestrator emits one ``WipeStep`` per row EXCEPT rows 5 and 11,
which fold into sibling steps because no standalone collaborator exists
for them (see the inline notes at those call sites):

  spec #1  -> "paperclip_company"     (PaperclipAdminClient.delete_company)
  spec #2  -> "paperclip_aurora_user" (paperclip_user_delete.delete_paperclip_user)
  spec #3  -> "ecs_service"           (ecs_manager.delete_user_service)
  spec #4  -> "efs_workspace_dir"     (Workspace.delete_user_dir)
  spec #5  -> folded into "ecs_service" (see note below)
  spec #6-7-> "bedrock_aips"          (bedrock_aip.delete_owner_aips)
  spec #8  -> "bedrock_role"          (bedrock_aip.delete_owner_bedrock_role)
  spec #9  -> "metronome"             (MetronomeClient.archive_customer)
  spec #10 -> "stripe"                (billing_service.delete_stripe_customer)
  spec #11 -> "usage_counters" / "paperclip_row" / "billing_row" / "user_row"
              (containers row + channel-links already folded into
              "ecs_service" — see note below)
  spec #12 -> "clerk_user"            (clerk_admin.delete_user) -- ALWAYS LAST

KNOWN GAP — NOT YET COVERED (honest, not "complete"): this orchestrator does
NOT wipe the ``integrations`` DDB table, the ``oauth-tokens`` table, the
``api-keys`` table, or the Secrets-Manager ``integration_secret_store``
(BYOK API keys + ChatGPT OAuth token material). No collaborator exists yet
for any of them. A trial_local account has none of these rows, so this does
not block the trial-account dogfood use case, but it IS a real gap for paid
Bedrock-managed, ChatGPT-OAuth, and BYOK accounts — those credentials
survive a wipe today. Follow-up work, not yet built.

Also NOT wiped: the ``company-slugs`` table (a wiped account keeps its slug
reservation permanently — there is no delete path, so the name can never be
handed to another owner) and the ``agent-email-addresses`` table (once
``AGENT_EMAIL_DOMAIN`` is live, a wiped account leaves its address row
pointing at a deleted owner, so inbound mail to that address has nowhere
valid to route). No collaborator exists yet for either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

import httpx

from core.auth import is_org_owner_id
from core.billing.bedrock_aip import delete_owner_aips, delete_owner_bedrock_role
from core.billing.metronome_client import for_settings as metronome_for_settings
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.repositories import billing_repo, container_repo, usage_repo, user_repo
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import clerk_admin
from core.services.billing_service import delete_stripe_customer
from core.services.paperclip_api_key import (
    delete_owner_agent_key_secrets,
    list_owner_agent_key_secret_names,
)
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

logger = logging.getLogger(__name__)

WipeStatus = Literal["ok", "skipped", "failed", "would_delete"]


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

    v1 supports personal owners only (owner_id == user_id, one Paperclip
    company, one Better Auth user) — org accounts have per-member fan-out
    (N Paperclip rows + N Better Auth users, shared company), which needs a
    looping per-member teardown that is a documented follow-up (design
    spec's "Out of scope: Org accounts"). The router (Task 4) turns this
    into a 400.
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

    def to_dict(self) -> dict:
        return {
            "owner_id": self.owner_id,
            "dry_run": self.dry_run,
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
    rows, and some of those rows get deleted by earlier steps."""

    user_row: dict | None
    container: dict | None
    paperclip: PaperclipCompany | None
    billing: dict | None


async def _read_identifiers(owner_id: str) -> _Identifiers:
    user_row = await user_repo.get(owner_id)
    container = await container_repo.get_by_owner_id(owner_id)
    paperclip = await PaperclipRepo(table_name="paperclip-companies").get(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    return _Identifiers(user_row=user_row, container=container, paperclip=paperclip, billing=billing)


def _refuse_org_owners(owner_id: str) -> None:
    if is_org_owner_id(owner_id):
        raise OrgWipeUnsupportedError(owner_id)


# ---------------------------------------------------------------------------
# Compound collaborators — wrapped so each is a single mockable async unit.
# ---------------------------------------------------------------------------
#
# Both PaperclipAdminClient.delete_company and MetronomeClient.archive_customer
# need an httpx.AsyncClient the caller owns the lifecycle of (mirrors
# build_paperclip_provisioning / metronome_gate.read_net_balance_microcents
# elsewhere in this codebase). Wrapping the open/call/close sequence here
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


async def _archive_metronome_customer(customer_id: str) -> None:
    """MetronomeClient.archive_customer via the settings-derived client
    (mirrors metronome_gate.read_net_balance_microcents's client lifecycle)."""
    async with httpx.AsyncClient(base_url=settings.METRONOME_BASE_URL, timeout=10.0) as http:
        await metronome_for_settings(http).archive_customer(customer_id=customer_id)


# ---------------------------------------------------------------------------
# Step-runner helpers
# ---------------------------------------------------------------------------


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
    steps: list[WipeStep] = []

    _preview_step(
        steps,
        system="paperclip_company",
        action="delete_company",
        exists=ids.paperclip is not None,
        detail_if_exists=f"company_id={ids.paperclip.company_id}" if ids.paperclip else "",
        detail_if_absent="no paperclip-companies row",
    )
    _preview_step(
        steps,
        system="paperclip_aurora_user",
        action="delete_paperclip_user",
        exists=ids.paperclip is not None,
        detail_if_exists=(f"paperclip_user_id={ids.paperclip.paperclip_user_id}" if ids.paperclip else ""),
        detail_if_absent="no paperclip-companies row",
    )
    _preview_step(
        steps,
        system="ecs_service",
        action="delete_user_service",
        exists=ids.container is not None,
        detail_if_exists=(
            f"service_name={(ids.container or {}).get('service_name', '')} "
            "(+ EFS access point, containers row, channel-links)"
        ),
        detail_if_absent="no containers row",
    )

    workspace_path = get_workspace().user_path(owner_id)
    workspace_exists = workspace_path.exists()
    _preview_step(
        steps,
        system="efs_workspace_dir",
        action="delete_user_dir",
        exists=workspace_exists,
        detail_if_exists=str(workspace_path),
        detail_if_absent="directory does not exist",
    )

    # O6 replica prefix — existence would need an S3 List per preview; the
    # delete is idempotent, so mirror the usage_counters convention: show
    # would_delete when the env replicates at all, skipped on the EFS layout.
    _preview_step(
        steps,
        system="litestream_replicas",
        action="delete_replica_prefix",
        exists=bool(settings.LITESTREAM_BUCKET),
        detail_if_exists=f"s3://{settings.LITESTREAM_BUCKET}/{owner_id}/",
        detail_if_absent="LITESTREAM_BUCKET unset (EFS layout)",
    )

    bedrock_aip_arns: dict = (ids.container or {}).get("bedrock_aip_arns") or {}
    _preview_step(
        steps,
        system="bedrock_aips",
        action="delete_owner_aips",
        exists=bool(bedrock_aip_arns),
        detail_if_exists=f"{len(bedrock_aip_arns)} AIP(s): {', '.join(bedrock_aip_arns.values())}",
        detail_if_absent="no bedrock_aip_arns on containers row",
    )

    bedrock_role_arn = (ids.container or {}).get("bedrock_role_arn")
    _preview_step(
        steps,
        system="bedrock_role",
        action="delete_owner_bedrock_role",
        exists=bool(bedrock_role_arn),
        detail_if_exists=bedrock_role_arn or "",
        detail_if_absent="no bedrock_role_arn on containers row",
    )

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

    _preview_step(
        steps,
        system="paperclip_row",
        action="paperclip_repo.delete",
        exists=ids.paperclip is not None,
        detail_if_exists="paperclip-companies row",
        detail_if_absent="no paperclip-companies row",
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
    steps.append(WipeStep(system="clerk_user", action="delete_user", status="would_delete", detail=owner_id))

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=True)


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
    steps: list[WipeStep] = []

    email = emails[0] if emails else ""
    env = settings.ENVIRONMENT or "dev"

    logger.info(
        "account_wipe.wipe_account: starting for owner=%s actor=%s",
        owner_id,
        actor_admin_id,
    )

    # --- Step 1 (spec #1): Paperclip company ------------------------------
    if ids.paperclip is not None:
        company_id = ids.paperclip.company_id

        async def _s1(company_id: str = company_id) -> str:
            await _delete_paperclip_company_call(company_id)
            return f"company_id={company_id}"

        await _step(steps, system="paperclip_company", action="delete_company", fn=_s1)
    else:
        _skip(steps, "paperclip_company", "delete_company", "no paperclip-companies row")

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
        _skip(steps, "paperclip_aurora_user", "delete_paperclip_user", "no paperclip-companies row")

    # --- Step 3 (spec #3 + #5 folded): ECS service ------------------------
    # ecs_manager.delete_user_service already does, in the right internal
    # order: scale-to-0 + delete ECS service (spec #3) -> deregister the
    # per-user task-def -> delete the per-user EFS access point (spec #5,
    # which the spec requires to run AFTER the ECS drain -- guaranteed here
    # because it's the same call) -> delete the containers row (part of
    # spec #11) -> sweep channel-links rows for this owner. There is no
    # standalone public method for the EFS-access-point delete alone
    # (EcsManager._delete_access_point is private and only ever called from
    # here), so this single WipeStep stands in for spec rows #3, #5, and the
    # containers-row/channel-links slice of #11.
    if ids.container is not None:

        async def _s3() -> str:
            await get_ecs_manager().delete_user_service(owner_id)
            return "ECS service + task-def + EFS access point + containers row + channel-links removed"

        await _step(steps, system="ecs_service", action="delete_user_service", fn=_s3)
    else:
        _skip(steps, "ecs_service", "delete_user_service", "no containers row")

    # --- Step 4 (spec #4): EFS workspace directory ------------------------
    # Unconditional -- Workspace.delete_user_dir is idempotent (no-ops if
    # the directory is already absent) and the directory's existence isn't
    # gated on the containers row.
    async def _s4() -> str:
        get_workspace().delete_user_dir(owner_id)
        return f"/mnt/efs/users/{owner_id}"

    await _step(steps, system="efs_workspace_dir", action="delete_user_dir", fn=_s4)

    # --- Step 4b: Litestream S3 replicas (O6 local-sqlite layout) ----------
    # The container boot script restores whatever survives under the owner's
    # replica prefix — leaving it behind resurrects pre-wipe sessions, device
    # trust, and auth_profile_store rows (live OAuth refresh tokens) on the
    # next provision. Idempotent; no-op while LITESTREAM_BUCKET is unset.
    async def _s4b() -> str:
        from core.services.litestream_replicas import delete_replica_prefix

        ok = await delete_replica_prefix(owner_id)
        if not ok:
            raise RuntimeError("replica prefix delete failed")
        return f"s3 prefix {owner_id}/"

    await _step(steps, system="litestream_replicas", action="delete_replica_prefix", fn=_s4b)

    # --- Step 5 (spec #6-7 folded): Bedrock AIPs ---------------------------
    # Pass the pre-fetched ARNs explicitly (from the identifier snapshot
    # taken at the top of this function) rather than letting delete_owner_aips
    # re-read the containers row: step 3 above (ecs_service) already deleted
    # that row as a side effect of ecs_manager.delete_user_service, so a live
    # re-read here would see nothing and silently no-op -- orphaning the
    # Bedrock inference profile + reverse row while still reporting "ok".
    bedrock_aip_arns: dict = (ids.container or {}).get("bedrock_aip_arns") or {}
    if bedrock_aip_arns:

        async def _s5(arns: list[str] = list(bedrock_aip_arns.values())) -> str:
            removed = await delete_owner_aips(owner_id, aip_arns=arns)
            return f"removed {len(removed)} AIP(s): {', '.join(removed)}"

        await _step(steps, system="bedrock_aips", action="delete_owner_aips", fn=_s5)
    else:
        _skip(steps, "bedrock_aips", "delete_owner_aips", "no bedrock_aip_arns on containers row")

    # --- Step 6 (spec #8): Bedrock owner role ------------------------------
    bedrock_role_arn = (ids.container or {}).get("bedrock_role_arn")
    if bedrock_role_arn:

        async def _s6(bedrock_role_arn: str = bedrock_role_arn) -> str:
            await delete_owner_bedrock_role(owner_id)
            return bedrock_role_arn

        await _step(steps, system="bedrock_role", action="delete_owner_bedrock_role", fn=_s6)
    else:
        _skip(steps, "bedrock_role", "delete_owner_bedrock_role", "no bedrock_role_arn on containers row")

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

    # --- Step 9 (spec #11, remaining tables) -------------------------------
    # containers + channel-links already removed inside step 3 above.

    async def _s9a() -> str:
        count = await usage_repo.delete_all_for_owner(owner_id)
        return f"deleted {count} row(s)"

    await _step(steps, system="usage_counters", action="delete_all_for_owner", fn=_s9a)

    if ids.paperclip is not None:

        async def _s9b() -> str:
            await PaperclipRepo(table_name="paperclip-companies").delete(owner_id)
            return "paperclip-companies row deleted"

        await _step(steps, system="paperclip_row", action="paperclip_repo.delete", fn=_s9b)
    else:
        _skip(steps, "paperclip_row", "paperclip_repo.delete", "no paperclip-companies row")

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
    # These live in Secrets Manager with no per-agent index (see the
    # paperclip_api_key module docstring), so the sweep enumerates
    # isol8/<env>/paperclip-agent-keys/<owner>/* by name prefix and deletes
    # each. Added after the 2026-07-14 account-wipe dogfood found them
    # orphaned post-wipe. Unconditional + idempotent (no-ops on an empty set).
    async def _s9e() -> str:
        count = await delete_owner_agent_key_secrets(owner_id)
        return f"deleted {count} per-agent key secret(s)"

    await _step(steps, system="paperclip_agent_keys", action="delete_owner_agent_key_secrets", fn=_s9e)

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
        "account_wipe.wipe_account: finished for owner=%s actor=%s ok=%d skipped=%d failed=%d",
        owner_id,
        actor_admin_id,
        sum(1 for s in steps if s.status == "ok"),
        sum(1 for s in steps if s.status == "skipped"),
        sum(1 for s in steps if s.status == "failed"),
    )

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=False)
