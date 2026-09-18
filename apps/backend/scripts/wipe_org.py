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

Runs INSIDE the backend task (ECS exec), same convention as
``roll_hermes_runtimes``/``migrate_workspace_to_hermes`` — it needs the EFS
mount for the ECS-era workspace delete, the VPC for Aurora/RDS Data API, and
the task role for ECS/S3/Secrets Manager:

    uv run python -m scripts.wipe_org --org org_XXX                    # preview
    uv run python -m scripts.wipe_org --org org_XXX --execute
    uv run python -m scripts.wipe_org --org org_XXX --execute --delete-clerk-users

PREVIEW IS THE DEFAULT. Without ``--execute`` nothing is deleted: every step
goes through ``_do`` below, which is the ONE call site that decides between
"print what I would touch" and "touch it", so the preview and the execution
can never disagree about what is in scope.

Exit codes: 0 all steps ok/skipped, 1 at least one step failed (each step is
isolated — a failure never stops the ones after it), 2 a refusal (checked for
EVERY named org before ANY org's first step, so a run either starts clean or
does nothing).

Idempotent: every collaborator here already treats "already gone" as success
(they were written for a retryable wipe), and the inventory read decides only
what to REPORT, never that a delete may be skipped.

WHAT IT DOES NOT TOUCH: the ``admin-actions`` table — it is the audit trail,
and this script APPENDS one row per executed org rather than deleting any.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.repositories import (
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
from core.repositories.paperclip_repo import PaperclipRepo, _org_create_claim_key
from core.services import clerk_admin, runtime_transport
from core.services.account_wipe import (
    WipeReport,
    WipeStep,
    _archive_metronome_customer,
    _archive_paperclip_member_call,
    _delete_paperclip_company_call,
    _skip,
    _step,
    _wipe_company_stripe,
)
from core.services.billing_service import delete_stripe_customer
from core.services.litestream_replicas import delete_replica_prefix
from core.services.oauth_service import PURPOSE_HARNESS, revoke_user_oauth
from core.services.owner_secret_sweep import delete_owner_secrets
from core.services.paperclip_api_key import delete_owner_agent_key_secrets
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

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
    """The Clerk organization, or None (404 / already deleted / no Clerk key)."""

    members: list[dict]
    """Clerk memberships — ``{"user_id", "email", ...}``, see clerk_admin."""

    paperclip_rows: list[dict] = field(default_factory=list)
    """Raw ``paperclip-companies`` rows for this org, via the by-org-id GSI.
    One per MEMBER (rows are keyed by Clerk user id, not by org id)."""

    container: dict | None = None
    billing: dict | None = None
    user_rows: dict[str, dict] = field(default_factory=dict)

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

    @property
    def emails(self) -> list[str]:
        out = [m["email"] for m in self.members if m.get("email")]
        out += [r["paperclip_email"] for r in self.paperclip_rows if r.get("paperclip_email")]
        return out

    def paperclip_row_for(self, user_id: str) -> dict | None:
        return next((r for r in self.paperclip_rows if r.get("user_id") == user_id), None)

    def email_for(self, user_id: str) -> str:
        """The member's address, Better Auth's copy first, Clerk's as fallback.

        Better Auth's ``user.email`` is written once at sign-up and never
        updated, so it is the one that matches the Aurora row — but rows
        written before ``paperclip_email`` existed carry "", and feeding that
        to ``delete_paperclip_user`` silently disarms ITS bootstrap-admin
        guard (the second line of defence behind ``check_refusals``).
        """
        row = self.paperclip_row_for(user_id) or {}
        if row.get("paperclip_email"):
            return row["paperclip_email"]
        return next((m.get("email") or "" for m in self.members if m.get("user_id") == user_id), "")

    @property
    def company_id(self) -> str:
        """The org's Paperclip company. Empty when the org has no rows (the
        member-less orphans) or only a bare seed-claim marker (company_id="")."""
        return next((r["company_id"] for r in self.paperclip_rows if r.get("company_id")), "")

    @property
    def runtime_host(self) -> str:
        return runtime_transport.host_for(self.container or {})


async def read_inventory(org_id: str) -> _Inventory:
    repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)
    inv = _Inventory(
        org_id=org_id,
        org=await clerk_admin.get_organization(org_id),
        members=await clerk_admin.list_organization_members(org_id),
        paperclip_rows=await repo.list_by_org_id(org_id),
        container=await container_repo.get_by_owner_id(org_id, consistent=True),
        billing=await billing_repo.get_by_owner_id(org_id, consistent_read=True),
    )
    for user_id in inv.member_ids:
        row = await user_repo.get(user_id)
        if row:
            inv.user_rows[user_id] = row
    return inv


async def check_refusals(inv: _Inventory, *, allow_multi_member: bool) -> None:
    """Every hard refusal, in cheapest-first order. Raises ``Refusal``."""
    org_id = inv.org_id
    if not is_org_owner_id(org_id):
        raise Refusal(f"{org_id!r} is not a Clerk org id — this script only wipes org-owned workspaces")
    if org_id in PROTECTED_ORG_IDS:
        raise Refusal(f"{org_id} is a protected org (founder / internal team) — never wiped by this script")

    member_ids = inv.member_ids
    if len(member_ids) > 1 and not allow_multi_member:
        raise Refusal(
            f"{org_id} has {len(member_ids)} members ({', '.join(member_ids)}) — "
            "pass --allow-multi-member to wipe a shared workspace"
        )

    protected = sorted({e for e in inv.emails if e.strip().lower() in BOOTSTRAP_EMAILS})
    if protected:
        raise Refusal(
            f"{org_id} has a bootstrap Paperclip admin member ({', '.join(protected)}) — "
            "deleting it breaks the admin session every provision depends on"
        )

    # Never delete a Paperclip company a SECOND workspace also names. Same
    # signal ``account_wipe._shared_company`` uses: the by-company-id GSI's
    # ACTIVE rows. Any active row belonging to a user who is not one of this
    # org's own members means another workspace is living in this company.
    if inv.company_id:
        repo = PaperclipRepo(table_name=_PAPERCLIP_TABLE)
        outsiders = sorted(set(await repo.get_user_ids_for_company(inv.company_id)) - set(member_ids))
        if outsiders:
            raise Refusal(
                f"{org_id}'s Paperclip company {inv.company_id} is also named by active row(s) for "
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


async def wipe_org(inv: _Inventory, *, execute: bool, delete_clerk_users: bool) -> WipeReport:
    """The ordered teardown. Mirrors ``account_wipe.wipe_account``'s step order
    and system names wherever the resource is the same, so a reader who knows
    one knows the other."""
    org_id = inv.org_id
    steps: list[WipeStep] = []
    env = settings.ENVIRONMENT or "dev"

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
                if row
                else f"user_id={user_id}: no paperclip-companies row"
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
    await _do(
        steps,
        system="paperclip_company",
        action="delete_company",
        execute=execute,
        present=bool(inv.company_id),
        detail=(
            f"company_id={inv.company_id}"
            if inv.company_id
            else "no paperclip-companies row for this org — nothing to resolve a company from"
        ),
        fn=lambda: _delete_company(inv.company_id),
    )

    # --- Step 3: the runtime ----------------------------------------------
    # Gate-token rows FIRST: the table is keyed on the token hash with no owner
    # index, so the containers row is the only handle on them — and the next
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

    # ONE call for both hosts: delete_user_service branches on runtime_host and
    # for an AgentCore row runs teardown_hermes_runtime (StopRuntimeSession,
    # DeleteAgentRuntime, revoke the bearer, delete the boot secret) plus — because
    # we pass delete_hermes_profiles=True, the terminal-caller flag — the whole
    # <org>/hermes/ S3 prefix. For an ECS row it scales to 0, deletes the service,
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
        fn=lambda: _delete_replicas(org_id),
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
    if execute:
        await _wipe_company_stripe(steps, owner_id=org_id)
    else:
        company_account_id = (inv.billing or {}).get("company_stripe_account_id") or ""
        await _do(
            steps,
            system="stripe",
            action="deauthorize_company_account",
            execute=False,
            present=bool(company_account_id),
            detail=company_account_id or "no company_stripe_account_id on billing row",
            fn=_noop,
        )
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
        await _do(
            steps,
            system="clerk_user",
            action="delete_user",
            execute=execute and delete_clerk_users,
            present=delete_clerk_users,
            detail=(user_id if delete_clerk_users else f"{user_id}: kept (pass --delete-clerk-users to delete)"),
            fn=lambda user_id=user_id: _delete_clerk_user(user_id),
        )

    return WipeReport(owner_id=org_id, steps=steps, dry_run=not execute)


# ---------------------------------------------------------------------------
# One thin wrapper per collaborator: each returns the report detail string.
# Module-level so a test can monkeypatch the name it wants without reaching
# into a closure.
# ---------------------------------------------------------------------------


async def _noop() -> str:  # pragma: no cover - preview-only placeholder, never called
    return ""


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


async def _delete_replicas(org_id: str) -> str:
    # Drain first, and fail rather than delete on a timeout: delete_user_service
    # only SUBMITS the scale-to-zero, and a still-live litestream can land an
    # upload AFTER the delete — silently repopulating the prefix so the next
    # provision restores state this script reported as erased.
    if not await get_ecs_manager().wait_until_drained(org_id, timeout_s=180.0):
        raise RuntimeError("container did not drain; refusing to delete replicas while a writer may still be live")
    if not await delete_replica_prefix(org_id):
        raise RuntimeError("replica prefix delete failed")
    return f"s3 prefix {org_id}/"


async def _archive_metronome(customer_id: str) -> str:
    await _archive_metronome_customer(customer_id)
    return customer_id


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
    await clerk_admin.delete_organization(org_id)
    return org_id


async def _delete_clerk_user(user_id: str) -> str:
    await clerk_admin.delete_user(user_id)
    return user_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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

    failed = 0
    for inv in inventories:
        started = time.monotonic()
        report = await wipe_org(inv, execute=args.execute, delete_clerk_users=args.delete_clerk_users)
        print_report(report)
        if args.execute:
            await _audit(report, int((time.monotonic() - started) * 1000))
        failed += sum(1 for s in report.steps if s.status == "failed")
    if not args.execute:
        print("\nPreview only — nothing was deleted. Re-run with --execute to commit.")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
