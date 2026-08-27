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
  (no spec row) -> "bedrock_gate_tokens" (bedrock_gate_tokens.delete_gate_token_rows)
  spec #3  -> "ecs_service"           (ecs_manager.delete_user_service)
  spec #4  -> "efs_workspace_dir"     (Workspace.delete_user_dir)
  spec #5  -> folded into "ecs_service" (see note below)
  spec #6-7-> folded into "ecs_service" (bedrock_aip.reap_owner_bedrock_resources)
  spec #8  -> folded into "ecs_service" (bedrock_aip.reap_owner_bedrock_resources)
  spec #9  -> "metronome"             (MetronomeClient.archive_customer)
  spec #10 -> "stripe"                (billing_service.delete_stripe_customer)
  spec #11 -> "usage_counters" / "paperclip_row" / "billing_row" / "user_row"
              (containers row + channel-links already folded into
              "ecs_service" — see note below; "paperclip_row" also carries the
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

KNOWN GAP — DDB-orphaned Paperclip state: both Paperclip steps resolve their
targets from the ``paperclip-companies`` DDB row, so an Aurora company whose
DDB row is missing survives the wipe. It is now reported loudly rather than
skipped silently (``_NO_PAPERCLIP_ROW`` + a warning log) — see that constant
for why enumerating from Aurora instead is not a safe fix.

Also NOT wiped: the ``company-slugs`` table (a wiped account keeps its slug
reservation permanently — there is no delete path, so the name can never be
handed to another owner) and the ``agent-email-addresses`` table (once
``AGENT_EMAIL_DOMAIN`` is live, a wiped account leaves its address row
pointing at a deleted owner, so inbound mail to that address has nowhere
valid to route). No collaborator exists yet for either.

The paperclip-companies table's OTHER prefixed key,
``__org_create_claim__:{org_id}`` (``kind="org_create_claim"``), is out of
reach by construction rather than by omission: it is keyed by Clerk org id and
only ever written on the org branch of ``ensure_user_ready``, so a v1
personal-owner wipe can never have created one. It becomes this orchestrator's
problem on the same day org wipe does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Literal

import httpx

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.billing.metronome_client import for_settings as metronome_for_settings
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.repositories import (
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    usage_repo,
    user_repo,
)
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo
from core.services import clerk_admin
from core.services.billing_service import delete_stripe_customer
from core.services.paperclip_api_key import (
    delete_owner_agent_key_secrets,
    list_owner_agent_key_secret_names,
)
from core.services.paperclip_admin_client import PaperclipAdminClient
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.oauth_service import PURPOSE_HARNESS, revoke_user_oauth
from core.services.owner_secret_sweep import delete_owner_secrets, list_owner_secret_names
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

logger = logging.getLogger(__name__)

WipeStatus = Literal["ok", "skipped", "failed", "would_delete"]

# Both Paperclip teardown steps resolve the company + Better Auth user from the
# DDB paperclip-companies row and NOTHING else, so an Aurora company whose DDB
# row never landed (partial provision, prior partial wipe, duplicate-signup
# debris) is left behind. That is not fixable by "enumerate from Aurora
# instead": Aurora carries no Lightbulb AI owner id, and every Lightbulb AI user is
# ``membership_role='owner'`` in every company they belong to (provision_org
# AND provision_member both invite with ``human_role="owner"``) — so an
# email-keyed lookup cannot tell an orphaned personal company from a LIVE org
# company the same human is a member of, and a fallback teardown built on it
# would delete other people's companies. Until Paperclip carries an owner-id
# attribution column, the honest move is to say so in the report instead of
# reporting a clean skip over rows nobody looked at.
_NO_PAPERCLIP_ROW = (
    "no paperclip-companies row — Aurora may still hold a company + Better Auth user for this "
    "owner (partial provision / prior partial wipe). Not resolvable from the owner id; check "
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
    inside the ``ecs_service`` step off the containers row's own ALL_OLD image,
    so an AIP minted mid-wipe is caught as long as the row named it when it
    died. Still snapshot-gated, and knowingly: the ``ecs_service`` step itself,
    and the ``billing_row`` / ``user_row`` / paperclip-company deletes.
    None of those rows is the sole handle on anything else, so the residue is
    findable and a re-run clears it.
    """

    user_row: dict | None
    container: dict | None
    paperclip: PaperclipCompany | None
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
    paperclip = await paperclip_repo.get(owner_id)
    pending_brief = await paperclip_repo.get_pending_brief(owner_id)
    billing = await billing_repo.get_by_owner_id(owner_id)
    return _Identifiers(
        user_row=user_row,
        container=container,
        paperclip=paperclip,
        billing=billing,
        pending_brief=pending_brief,
    )


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
        detail_if_absent=_NO_PAPERCLIP_ROW,
    )
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
        system="ecs_service",
        action="delete_user_service",
        exists=ids.container is not None,
        detail_if_exists=(
            f"service_name={(ids.container or {}).get('service_name', '')} "
            "(+ EFS access point, containers row, channel-links, "
            "and the Bedrock footprint: gate-token rows, AIPs, IAM role)"
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

    # Litestream replica prefix — existence would need an S3 List per preview; the
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

    # No standalone bedrock_aips / bedrock_role preview steps: the executed wipe
    # no longer has them either. Both are reaped inside the ecs_service step, by
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

    paperclip_rows = [
        label
        for label, present in (
            ("paperclip-companies row", ids.paperclip is not None),
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
        # LOUD, not silent: the report line alone is easy to read as "this
        # owner had no Paperclip side" when it can equally mean "the handle to
        # it is missing". See _NO_PAPERCLIP_ROW.
        logger.warning(
            "account_wipe: owner=%s has no paperclip-companies row — Paperclip company + Better "
            "Auth user teardown SKIPPED; Aurora may still hold rows for this owner",
            owner_id,
        )
        _skip(steps, "paperclip_company", "delete_company", _NO_PAPERCLIP_ROW)

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
            row_at_death = await get_ecs_manager().delete_user_service(owner_id)
            detail = "ECS service + task-def + EFS access point + containers row + channel-links removed"
            # FINAL SWEEP — the last moment any gate-token hash is reachable.
            # ``delete_user_service`` returns the row's own ALL_OLD image, so
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

    # --- Step 4b: Litestream S3 replicas (local-sqlite layout) ----------
    # The container boot script restores whatever survives under the owner's
    # replica prefix — leaving it behind resurrects pre-wipe sessions, device
    # trust, and auth_profile_store rows (live OAuth refresh tokens) on the
    # next provision. Idempotent; no-op while LITESTREAM_BUCKET is unset.
    async def _s4b() -> str:
        from core.services.litestream_replicas import delete_replica_prefix

        # delete_user_service only SUBMITS the scale-to-zero + delete; a still
        # running container's litestream can land an in-flight upload AFTER we
        # delete, silently repopulating the prefix so the next provision
        # restores the state this wipe reported as erased. Drain first, and
        # treat a drain timeout as a FAILURE rather than deleting anyway: a
        # visible, retryable failed step beats a racy delete reported as
        # success. Budget exceeds the container's stopTimeout (120s) so a
        # normal full-length drain does not read as a timeout.
        if not settings.LITESTREAM_BUCKET:
            return "skipped (EFS layout — no replicas)"
        # NOT gated on ids.container: delete_user_service removes the row
        # before this step, so a retried wipe sees container=None while the
        # task may still be draining. wait_until_drained returns immediately
        # when the service is already gone, so the unconditional call is cheap.
        if not await get_ecs_manager().wait_until_drained(owner_id, timeout_s=180.0):
            raise RuntimeError("container did not drain; refusing to delete replicas while a writer may still be live")
        ok = await delete_replica_prefix(owner_id)
        if not ok:
            raise RuntimeError("replica prefix delete failed")
        return f"s3 prefix {owner_id}/"

    await _step(steps, system="litestream_replicas", action="delete_replica_prefix", fn=_s4b)

    # --- Steps 5-6 (spec #6-7-8): Bedrock AIPs + owner role ----------------
    # Deliberately NOT separate steps any more. Both are reaped inside step 3's
    # ``delete_user_service`` (see bedrock_aip.reap_owner_bedrock_resources),
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

    # --- Step 9 (spec #11, remaining tables) -------------------------------
    # containers + channel-links already removed inside step 3 above.

    async def _s9a() -> str:
        count = await usage_repo.delete_all_for_owner(owner_id)
        return f"deleted {count} row(s)"

    await _step(steps, system="usage_counters", action="delete_all_for_owner", fn=_s9a)

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
            deleted.append("paperclip-companies row deleted")
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
    # These live in Secrets Manager with no per-agent index (see the
    # paperclip_api_key module docstring), so the sweep enumerates
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
        # The coding-harness grant is a SECOND, independent token row under
        # a suffixed key ({owner}#harness) — the bare-key deletion above
        # never touches it, and leaving it would strand encrypted
        # long-lived tokens after the Clerk identity is gone. (Its EFS
        # auth.json is covered by the workspace deletion step.)
        await revoke_user_oauth(user_id=owner_id, purpose=PURPOSE_HARNESS)
        return "stored chatgpt oauth tokens deleted, provider + harness rows (grant NOT revoked upstream at OpenAI)"

    await _step(steps, system="oauth_tokens", action="revoke_user_oauth", fn=_s9h)

    async def _s9i() -> str:
        addresses = await agent_email_repo.delete_all_for_owner(owner_id)
        return f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}"

    await _step(steps, system="agent_email_addresses", action="delete_all_for_owner", fn=_s9i)

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
        "account_wipe.wipe_account: finished for owner=%s actor=%s ok=%d skipped=%d failed=%d",
        owner_id,
        actor_admin_id,
        sum(1 for s in steps if s.status == "ok"),
        sum(1 for s in steps if s.status == "skipped"),
        sum(1 for s in steps if s.status == "failed"),
    )

    return WipeReport(owner_id=owner_id, steps=steps, dry_run=False)
