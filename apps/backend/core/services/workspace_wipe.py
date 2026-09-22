"""Admin Workspace-Wipe orchestrator (ORG-keyed).

Companion to ``core.services.account_wipe`` (USER-keyed). The user-keyed wipe
snapshots rows keyed by the user id (``user_repo.get``,
``container_repo.get_by_owner_id(user_id)``, per-user Paperclip rows) — but
for org workspaces the containers row, billing row, integrations, gate rows
and messaging OWNER partition all live under the ORG id. A user-keyed wipe of
a workspace member therefore misses the runtime entirely ("skipped: no
containers row") while still destroying every user-keyed row. On 2026-09-21
that half-wipe ran live against a workspace mid-incident.

This module snapshots by ORG id instead:

* ``preview_workspace_wipe(org_id)`` — READ-ONLY. One snapshot, one step
  registry (``WORKSPACE_STEP_REGISTRY``), every step ``would_delete`` /
  ``skipped``. Calls NO delete method. Returns a ``WorkspaceWipeReport``
  carrying a ``fingerprint`` of the snapshot it was built from.
* ``wipe_workspace(org_id, ...)`` — re-snapshots FRESH, aborts on drift when
  the caller passes the preview's ``expected_fingerprint``
  (``StaleWorkspacePreviewError``), then runs each SELECTED layer's steps in
  the registry's order, each in its own try/except (partial-failure tolerance
  is inherited from ``account_wipe._step``: a failed step records
  ``status="failed"`` and execution CONTINUES, Clerk deletes last).

Layers (each independently selectable): ``runtime`` (gate tokens, AgentCore
runtime + S3 profiles + containers row, provisioning workflows,
usage counters), ``paperclip`` (company, per-member Aurora users, per-member
DDB rows + pending briefs, agent-key secrets), ``messaging`` (room partitions
+ owner partition), ``secrets`` (owner secrets, integrations, OAuth tokens,
agent email, slack installs), ``identity`` (per-member users rows, slug,
per-member Clerk users, Clerk org — ALWAYS LAST), ``billing`` (Metronome,
Stripe customer + company-stripe deauthorize, billing row).

Billing/Stripe default PRESERVE: the billing layer runs only with
``include_billing=True`` AND ``confirm_billing="DELETE-BILLING"``. Anything
else records its steps ``skipped`` with a "preserved by default" detail and a
warning log — money must never move on an accidents-shaped default.

Preview + execute share ONE step registry (``WORKSPACE_STEP_REGISTRY``): both
iterate ``_selected_defs`` and call the same ``preview`` / ``run`` closures
on each def. A step that exists in one but not the other is the divergence
``account_wipe._Identifiers`` warns about; the registry makes it structural.
``test_workspace_step_registry_covers_user_path`` asserts the registry covers
every system the user-keyed path emits, so the two cannot silently diverge.

Snapshot staleness (the same rule as ``account_wipe._Identifiers``): the
snapshot CARRIES values a later step can no longer read (ARNs off a row an
earlier step deletes) and shapes the preview. It never decides a delete can
be skipped — the gate-token step re-reads STRONGLY CONSISTENT and deletes the
union, the company-stripe step re-reads billing fresh, messaging re-lists
room ids at execute time, and pending-brief deletes are always attempted per
member. The top-level fingerprint covers provision-shaped drift between
preview and execute (a new room, a rotated secret name, a member join);
in-flight row CONTENT changes inside an unchanged shape are closed by those
per-step re-reads.

No secret VALUES anywhere: previews carry counts, ARNs, and secret NAMES —
never values. No prod writes outside the wipe itself; no dispatches.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.config import settings
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
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin, company_stripe
from core.services.account_wipe import (
    BootstrapAccountProtectedError,
    WipeReport,
    WipeStep,
    _archive_metronome_customer,
    _delete_paperclip_company_call,
    _NO_PAPERCLIP_ROW,
    _preview_step,
    _refuse_bootstrap_account,
    _skip,
    _step,
    cancel_open_workflows,
)
from core.services.billing_service import delete_stripe_customer
from core.services.hermes_provisioning import delete_workspace_runtime
from core.services.oauth_service import PURPOSE_HARNESS, revoke_user_oauth
from core.services.owner_secret_sweep import delete_owner_secrets, list_owner_secret_names
from core.services.paperclip_api_key import (
    delete_owner_agent_key_secrets,
    list_owner_agent_key_secret_names,
)
from core.services.paperclip_user_delete import BOOTSTRAP_EMAILS, delete_paperclip_user

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layers
# ---------------------------------------------------------------------------

LAYER_RUNTIME = "runtime"
LAYER_PAPERCLIP = "paperclip"
LAYER_MESSAGING = "messaging"
LAYER_SECRETS = "secrets"
LAYER_IDENTITY = "identity"
LAYER_BILLING = "billing"

ALL_LAYERS: tuple[str, ...] = (
    LAYER_RUNTIME,
    LAYER_PAPERCLIP,
    LAYER_MESSAGING,
    LAYER_SECRETS,
    LAYER_IDENTITY,
    LAYER_BILLING,
)

#: Everything a workspace wipe deletes unless the operator narrows it.
#: Billing is NEVER in the default: money moves only on explicit override.
DEFAULT_LAYERS: tuple[str, ...] = tuple(layer for layer in ALL_LAYERS if layer != LAYER_BILLING)

#: The typed override that arms the billing layer. A bool is one misclick;
#: a phrase is a decision.
BILLING_CONFIRM_PHRASE = "DELETE-BILLING"


class WorkspaceWipeError(Exception):
    """Base for workspace-wipe refusals (router maps these to 4xx)."""


class WorkspaceConfirmMismatchError(WorkspaceWipeError):
    """The typed org confirmation did not match the resolved workspace."""


class AmbiguousWorkspaceError(WorkspaceWipeError):
    """A user id resolves to zero or several workspaces and no explicit org
    confirmation disambiguated it."""


class BillingPreservedError(WorkspaceWipeError):
    """Billing-layer delete attempted without the explicit override."""


class StaleWorkspacePreviewError(WorkspaceWipeError):
    """Execute's fresh snapshot drifted from the preview's fingerprint."""

    def __init__(self, expected: str, actual: str):
        super().__init__(
            "Workspace changed since the preview was taken "
            f"(preview={expected[:12]} now={actual[:12]}); re-run preview and confirm again."
        )
        self.expected = expected
        self.actual = actual


# ---------------------------------------------------------------------------
# Snapshot (by ORG id)
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceSnapshot:
    """Every handle an org-keyed wipe needs, read up front.

    See the module docstring (and ``account_wipe._Identifiers``) for what this
    snapshot IS (a carrier of values later steps can no longer read + the
    preview's shape) and IS NOT (a decision that a delete can be skipped).
    """

    org_id: str
    workspace_name: str | None
    container: dict | None
    billing: dict | None
    member_rows: list[dict]
    member_emails: dict[str, str]
    company_id: str | None
    room_ids: list[str]
    secret_names: list[str]
    agent_key_names: list[str]
    integration_rows: list[dict]
    email_rows: list[dict]
    slack_rows: list[dict]
    slug_row: dict | None
    open_workflows: list[dict]
    gate_hashes: list[str]
    fingerprint: str = ""


def member_user_ids(snapshot: WorkspaceSnapshot) -> list[str]:
    """Deduped member user ids, snapshot order."""
    return list(dict.fromkeys(r.get("user_id") for r in snapshot.member_rows if r.get("user_id")))


async def _read_workspace_snapshot(org_id: str) -> WorkspaceSnapshot:
    container = await container_repo.get_by_owner_id(org_id)
    billing = await billing_repo.get_by_owner_id(org_id)
    paperclip_repo = PaperclipRepo(table_name="paperclip-companies")
    member_rows = await paperclip_repo.list_by_org_id(org_id)
    company_id = await paperclip_repo.get_org_company_id(org_id)
    if not company_id:
        for row in member_rows:
            if row.get("company_id"):
                company_id = row["company_id"]
                break
    member_emails: dict[str, str] = {}
    for row in member_rows:
        uid = row.get("user_id")
        if not uid or uid in member_emails:
            continue
        try:
            user_row = await user_repo.get(uid)
        except Exception:  # noqa: BLE001 -- best-effort; execute re-checks per member
            user_row = None
        if user_row and user_row.get("email"):
            member_emails[uid] = user_row["email"]
    room_ids = await messaging_repo.list_owner_room_ids(org_id)
    secret_names = await list_owner_secret_names(org_id)
    agent_key_names = await list_owner_agent_key_secret_names(org_id)
    integration_rows = await integrations_repo.list_for_owner(org_id)
    email_rows = await agent_email_repo.query_by_owner(org_id)
    slack_rows = await slack_installs_repo.list_for_owner(org_id)
    slug_row = await company_slug_repo.get_by_owner(org_id)
    workflows = await provisioning_workflow_repo.list_for_owner(org_id)
    open_workflows = [r for r in workflows if r.get("state") not in provisioning_workflow_repo.TERMINAL_STATES]
    org = await clerk_admin.get_organization(org_id)
    snapshot = WorkspaceSnapshot(
        org_id=org_id,
        workspace_name=(org or {}).get("name"),
        container=container,
        billing=billing,
        member_rows=member_rows,
        member_emails=member_emails,
        company_id=company_id,
        room_ids=room_ids,
        secret_names=secret_names,
        agent_key_names=agent_key_names,
        integration_rows=integration_rows,
        email_rows=email_rows,
        slack_rows=slack_rows,
        slug_row=slug_row,
        open_workflows=open_workflows,
        gate_hashes=owner_gate_token_hashes(container),
    )
    snapshot.fingerprint = snapshot_fingerprint(snapshot)
    return snapshot


def snapshot_fingerprint(snapshot: WorkspaceSnapshot) -> str:
    """Stable hash of the snapshot's SHAPE (ids, counts, names — no values).

    Preview stamps this onto its report; execute re-snapshots and aborts on
    drift. Sorted + JSON-canonical so key order and DDB page order cannot
    false-positive a re-preview.
    """
    canonical = {
        "org_id": snapshot.org_id,
        "agentcore_runtime_arn": (snapshot.container or {}).get("agentcore_runtime_arn", ""),
        "billing_ids": {
            k: bool((snapshot.billing or {}).get(k))
            for k in ("metronome_customer_id", "stripe_customer_id", "company_stripe_account_id")
        },
        "billing_row": snapshot.billing is not None,
        "company_id": snapshot.company_id or "",
        "member_ids": sorted(member_user_ids(snapshot)),
        "room_ids": sorted(snapshot.room_ids),
        "secret_names": sorted(snapshot.secret_names),
        "agent_key_names": sorted(snapshot.agent_key_names),
        "integrations": sorted(r.get("provider", "?") for r in snapshot.integration_rows),
        "agent_emails": sorted(r.get("address", "?") for r in snapshot.email_rows),
        "slack_teams": sorted(r.get("team_id", "?") for r in snapshot.slack_rows),
        "slug": (snapshot.slug_row or {}).get("slug", ""),
        "open_workflows": sorted(r.get("workflow_id", "?") for r in snapshot.open_workflows),
        "gate_hash_count": len(snapshot.gate_hashes),
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# User -> org resolution (explicit confirmation required on ambiguity)
# ---------------------------------------------------------------------------


async def resolve_workspace_org(user_id: str, confirm_org: str | None = None) -> str:
    """Resolve which workspace ``user_id``'s wipe means.

    Sources: the user's Paperclip row (``org_id``) + Clerk memberships. When
    they differ — or the user sits in several workspaces — there is no safe
    guess: the caller must pass ``confirm_org`` naming the intended org
    explicitly, else ``AmbiguousWorkspaceError``. A ``confirm_org`` that
    matches NO known membership raises ``WorkspaceConfirmMismatchError``.
    """
    candidates: list[str] = []
    try:
        paperclip_row = await PaperclipRepo(table_name="paperclip-companies").get(user_id)
    except Exception:  # noqa: BLE001 -- resolution must not fail closed on a DDB blip
        paperclip_row = None
    if paperclip_row is not None and is_org_owner_id(paperclip_row.org_id):
        candidates.append(paperclip_row.org_id)
    try:
        memberships = await clerk_admin.list_user_organizations(user_id)
    except Exception:  # noqa: BLE001 -- same posture as the read above
        memberships = []
    for m in memberships:
        oid = m.get("id")
        if oid and oid not in candidates:
            candidates.append(oid)
    if confirm_org:
        if candidates and confirm_org not in candidates:
            raise WorkspaceConfirmMismatchError(
                f"{user_id} is not a member of {confirm_org} (known workspaces: {', '.join(candidates)}); refusing."
            )
        return confirm_org
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AmbiguousWorkspaceError(f"No workspace found for {user_id}; pass confirm_org with the workspace org id.")
    raise AmbiguousWorkspaceError(
        f"{user_id} belongs to several workspaces ({', '.join(candidates)}); "
        "pass confirm_org with the intended workspace org id."
    )


# ---------------------------------------------------------------------------
# Step registry — the ONE list preview + execute share
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceStepDef:
    """One wipe step: its identity, its layer gate, and both behaviours.

    ``preview`` is READ-ONLY and returns (exists, detail). ``run`` performs
    the delete and returns a detail string. Preview and execute each run one
    loop over the same ``WORKSPACE_STEP_REGISTRY`` in the same order — a step
    cannot exist in one and not the other.
    """

    system: str
    action: str
    layer: str
    preview: Callable[[WorkspaceSnapshot], tuple[bool, str]]
    run: Callable[[WorkspaceSnapshot], Awaitable[str]]


def _company_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.company_id:
        return False, _NO_PAPERCLIP_ROW
    members = member_user_ids(snap)
    return True, f"company_id={snap.company_id} ({len(members)} member(s): {', '.join(members) or 'none'})"


async def _company_run(snap: WorkspaceSnapshot) -> str:
    if not snap.company_id:
        return "no paperclip company for workspace " + _NO_PAPERCLIP_ROW
    await _delete_paperclip_company_call(snap.company_id)
    return f"company_id={snap.company_id}"


def _aurora_users_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    ids = [r.get("paperclip_user_id", "?") for r in snap.member_rows if r.get("paperclip_user_id")]
    if not ids:
        return False, _NO_PAPERCLIP_ROW
    return True, f"{len(ids)} Better Auth user(s): {', '.join(ids)}"


async def _aurora_users_run(snap: WorkspaceSnapshot) -> str:
    env = settings.ENVIRONMENT or "dev"
    failures: list[str] = []
    deleted = 0
    for row in snap.member_rows:
        pc_user_id = row.get("paperclip_user_id")
        uid = row.get("user_id", "?")
        if not pc_user_id:
            failures.append(f"{uid}: no paperclip_user_id on row")
            continue
        try:
            await delete_paperclip_user(paperclip_user_id=pc_user_id, email=snap.member_emails.get(uid, ""), env=env)
            deleted += 1
        except Exception as e:  # noqa: BLE001 -- per-member partial failure, aggregated below
            failures.append(f"{uid}: {e}")
    if failures:
        raise RuntimeError(f"deleted {deleted} Better Auth user(s); failures: {'; '.join(failures)}")
    if not deleted:
        return "no Better Auth users for workspace"
    return f"deleted {deleted} Better Auth user(s)"


def _gate_tokens_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.gate_hashes:
        return False, "no bedrock-gate token hash on containers row"
    return True, f"{len(snap.gate_hashes)} gate-token row(s)"


async def _gate_tokens_run(snap: WorkspaceSnapshot) -> str:
    # Same union rule as the user-keyed step 2b: the snapshot predates every
    # delete, so re-read strongly consistent and delete the union.
    fresh = await container_repo.get_by_owner_id(snap.org_id, consistent=True)
    hashes = list(dict.fromkeys([*snap.gate_hashes, *owner_gate_token_hashes(fresh)]))
    if not hashes:
        return "no bedrock-gate token hash on containers row"
    deleted = await delete_gate_token_rows(hashes)
    return f"deleted {deleted} of {len(hashes)} gate-token row(s)"


def _runtime_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if snap.container is None:
        return False, "no containers row"
    arn = (snap.container or {}).get("agentcore_runtime_arn", "")
    return True, (
        f"agentcore_runtime_arn={arn} (+ boot secret, S3 profiles under "
        f"{snap.org_id}/hermes/, containers row, channel-links, Bedrock footprint)"
    )


async def _runtime_run(snap: WorkspaceSnapshot) -> str:
    if snap.container is None:
        # Re-read: a provision racing the wipe may have created the row after
        # the snapshot. The seam deletes unconditionally, so call it anyway —
        # it is idempotent on a missing row and returns None.
        row_at_death = await delete_workspace_runtime(snap.org_id, delete_profiles=True)
        if row_at_death is None:
            return "no containers row"
    else:
        row_at_death = await delete_workspace_runtime(snap.org_id, delete_profiles=True)
    detail = "agentcore runtime + boot secret + S3 profiles + containers row + channel-links removed"
    late_hashes = owner_gate_token_hashes(row_at_death)
    swept = await delete_gate_token_rows(late_hashes) if late_hashes else 0
    if swept:
        detail += f" (+ swept {swept} gate-token row(s) still named on the row at delete time)"
    return detail


def _metronome_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    cid = (snap.billing or {}).get("metronome_customer_id")
    if not cid:
        return False, "no metronome_customer_id on billing row"
    return True, str(cid)


async def _metronome_run(snap: WorkspaceSnapshot) -> str:
    cid = (snap.billing or {}).get("metronome_customer_id")
    if not cid:
        return "no metronome_customer_id on billing row"
    await _archive_metronome_customer(cid)
    return str(cid)


def _stripe_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    cid = (snap.billing or {}).get("stripe_customer_id")
    if not cid:
        return False, "no stripe_customer_id on billing row"
    return True, str(cid)


async def _stripe_run(snap: WorkspaceSnapshot) -> str:
    deleted = await delete_stripe_customer(snap.org_id)
    cid = (snap.billing or {}).get("stripe_customer_id") or "?"
    return f"stripe_customer_id={cid} deleted={deleted}"


def _company_stripe_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    acct = (snap.billing or {}).get("company_stripe_account_id")
    if not acct:
        return False, "no company_stripe_account_id on billing row"
    return True, f"{acct} (deauthorized only — the business account is retained by the owner)"


async def _company_stripe_run(snap: WorkspaceSnapshot) -> str:
    # Fresh read: everything above is slow and an OAuth callback landing
    # mid-wipe binds an account the snapshot cannot know about.
    row = await billing_repo.get_by_owner_id(snap.org_id, consistent_read=True) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        return "no company_stripe_account_id on billing row"
    if not await company_stripe.release_and_unbind(
        owner_id=snap.org_id, account_id=account_id, source=row.get("company_stripe_source")
    ):
        raise RuntimeError(f"unbound {account_id} locally, but Stripe did not confirm the revoke")
    return f"company_stripe_account_id={account_id} deauthorized (account retained by owner)"


async def _usage_run(snap: WorkspaceSnapshot) -> str:
    count = await usage_repo.delete_all_for_owner(snap.org_id)
    return f"deleted {count} row(s)"


async def _messaging_run(snap: WorkspaceSnapshot) -> str:
    # Fresh re-list: a message racing the wipe creates rooms the snapshot
    # predates. Keep the owner catalog until every room partition is gone so a
    # partial failure retries from a complete inventory.
    room_ids = await messaging_repo.list_owner_room_ids(snap.org_id)
    deleted = 0
    for room_id in room_ids:
        meta = await messaging_repo.get_room(room_id)
        if meta is not None and meta.get("ownerId") != snap.org_id:
            raise RuntimeError("messaging owner catalog referenced a room owned elsewhere")
        deleted += await messaging_repo.delete_room_partition(room_id)
    deleted += await messaging_repo.delete_owner_partition(snap.org_id)
    return f"deleted {len(room_ids)} room partition(s) and {deleted} messaging row(s)"


def _paperclip_rows_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    parts = []
    if snap.member_rows:
        parts.append(f"{len(snap.member_rows)} paperclip-companies row(s)")
    # Pending briefs are per-member prefixed items; count what the snapshot
    # knows, but execute always attempts each member's (idempotent).
    if parts:
        return True, " + ".join(parts) + " (+ per-member pending_brief items, always attempted)"
    return False, "no paperclip-companies rows for workspace"


async def _paperclip_rows_run(snap: WorkspaceSnapshot) -> str:
    repo = PaperclipRepo(table_name="paperclip-companies")
    deleted: list[str] = []
    for uid in member_user_ids(snap):
        await repo.delete(uid)
        if await repo.delete_pending_brief(uid):
            deleted.append(f"{uid}: row + pending_brief")
        else:
            deleted.append(f"{uid}: row")
    if not deleted:
        # Still attempt the org id itself: a funnel answer can sit under it.
        if await repo.delete_pending_brief(snap.org_id):
            return "pending_brief row deleted"
        return "no paperclip-companies or pending_brief rows"
    return "deleted " + "; ".join(deleted)


async def _billing_row_run(snap: WorkspaceSnapshot) -> str:
    if snap.billing is None:
        # Unconditional attempt: the snapshot predates the Stripe steps and a
        # billing row created mid-wipe must not survive. Delete is idempotent.
        await billing_repo.delete(snap.org_id)
        return "no billing-accounts row"
    await billing_repo.delete(snap.org_id)
    return "billing-accounts row deleted"


async def _user_rows_run(snap: WorkspaceSnapshot) -> str:
    members = member_user_ids(snap)
    for uid in members:
        await user_repo.delete(uid)
    if not members:
        return "no users rows for workspace"
    return f"deleted {len(members)} users row(s)"


def _agent_keys_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.agent_key_names:
        return False, "no per-agent Paperclip key secrets"
    return True, f"{len(snap.agent_key_names)} per-agent Paperclip key secret(s)"


async def _agent_keys_run(snap: WorkspaceSnapshot) -> str:
    total = await delete_owner_agent_key_secrets(snap.org_id)
    for uid in member_user_ids(snap):
        total += await delete_owner_agent_key_secrets(uid)
    return f"deleted {total} per-agent key secret(s)"


async def _provisioning_run(snap: WorkspaceSnapshot) -> str:
    cancelled, open_count = await cancel_open_workflows(snap.org_id)
    return f"cancelled {cancelled} of {open_count} open provisioning workflow(s)"


def _owner_secrets_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.secret_names:
        return False, "no owner secrets"
    return True, f"{len(snap.secret_names)} secret(s): BYOK keys, integration creds, OAuth apps (names only)"


async def _owner_secrets_run(snap: WorkspaceSnapshot) -> str:
    names = await delete_owner_secrets(snap.org_id)
    for uid in member_user_ids(snap):
        names += await delete_owner_secrets(uid)
    return f"deleted {len(names)} owner secret(s) (BYOK keys, integration creds, OAuth apps)"


def _integrations_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.integration_rows:
        return False, "no integration rows"
    return True, f"{len(snap.integration_rows)} integration row(s)"


async def _integrations_run(snap: WorkspaceSnapshot) -> str:
    providers = await integrations_repo.delete_all_for_owner(snap.org_id)
    for uid in member_user_ids(snap):
        providers += await integrations_repo.delete_all_for_owner(uid)
    return f"deleted {len(providers)} integration row(s): {', '.join(providers) or 'none'}"


async def _oauth_run(snap: WorkspaceSnapshot) -> str:
    targets = [snap.org_id, *member_user_ids(snap)]
    for uid in targets:
        await revoke_user_oauth(user_id=uid)
        await revoke_user_oauth(user_id=uid, purpose=PURPOSE_HARNESS)
    return f"stored chatgpt oauth tokens deleted for {len(targets)} principal(s) (grant NOT revoked upstream)"


def _agent_email_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.email_rows:
        return False, "no agent email addresses"
    addrs = ", ".join(r.get("address", "?") for r in snap.email_rows)
    return True, f"{len(snap.email_rows)} address(es): {addrs}"


async def _agent_email_run(snap: WorkspaceSnapshot) -> str:
    addresses = await agent_email_repo.delete_all_for_owner(snap.org_id)
    return f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}"


async def _slack_run(snap: WorkspaceSnapshot) -> str:
    teams = await slack_installs_repo.delete_all_for_owner(snap.org_id)
    return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"


def _slug_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if snap.slug_row is None:
        return False, "no slug reservation"
    return True, f"slug {(snap.slug_row or {}).get('slug')}"


async def _slug_run(snap: WorkspaceSnapshot) -> str:
    slug = await company_slug_repo.delete_for_owner(snap.org_id)
    for uid in member_user_ids(snap):
        member_slug = await company_slug_repo.delete_for_owner(uid)
        slug = slug or member_slug
    return f"released slug {slug}" if slug else "no slug reservation"


async def _clerk_users_run(snap: WorkspaceSnapshot) -> str:
    members = member_user_ids(snap)
    failures: list[str] = []
    deleted = 0
    for uid in members:
        try:
            await clerk_admin.delete_user(uid)
            deleted += 1
        except Exception as e:  # noqa: BLE001 -- per-member partial failure
            failures.append(f"{uid}: {e}")
    if failures:
        raise RuntimeError(f"deleted {deleted} Clerk user(s); failures: {'; '.join(failures)}")
    if not deleted:
        return "no member Clerk users"
    return f"deleted {deleted} member Clerk user(s)"


async def _clerk_org_run(snap: WorkspaceSnapshot) -> str:
    # ALWAYS LAST: leaving the org alive lets everything regrow on the next
    # /container/status poll.
    await clerk_admin.delete_organization(snap.org_id)
    return snap.org_id


WORKSPACE_STEP_REGISTRY: tuple[WorkspaceStepDef, ...] = (
    WorkspaceStepDef("paperclip_company", "delete_company", LAYER_PAPERCLIP, _company_preview, _company_run),
    WorkspaceStepDef(
        "paperclip_aurora_user", "delete_paperclip_user", LAYER_PAPERCLIP, _aurora_users_preview, _aurora_users_run
    ),
    WorkspaceStepDef(
        "bedrock_gate_tokens", "delete_gate_token_rows", LAYER_RUNTIME, _gate_tokens_preview, _gate_tokens_run
    ),
    WorkspaceStepDef("runtime", "delete_workspace_runtime", LAYER_RUNTIME, _runtime_preview, _runtime_run),
    WorkspaceStepDef("metronome", "archive_customer", LAYER_BILLING, _metronome_preview, _metronome_run),
    WorkspaceStepDef("stripe", "delete_stripe_customer", LAYER_BILLING, _stripe_preview, _stripe_run),
    WorkspaceStepDef(
        "stripe", "deauthorize_company_account", LAYER_BILLING, _company_stripe_preview, _company_stripe_run
    ),
    WorkspaceStepDef(
        "usage_counters",
        "delete_all_for_owner",
        LAYER_RUNTIME,
        lambda snap: (True, "all usage-counter rows for workspace"),
        _usage_run,
    ),
    WorkspaceStepDef(
        "messaging",
        "delete_owner_rooms_and_streams",
        LAYER_MESSAGING,
        lambda snap: (
            True,
            f"{len(snap.room_ids)} owned room partition(s), then owner catalog and private user-stream mappings",
        ),
        _messaging_run,
    ),
    WorkspaceStepDef(
        "paperclip_row", "paperclip_repo.delete", LAYER_PAPERCLIP, _paperclip_rows_preview, _paperclip_rows_run
    ),
    WorkspaceStepDef(
        "billing_row",
        "billing_repo.delete",
        LAYER_BILLING,
        lambda snap: (True, "billing-accounts row") if snap.billing is not None else (False, "no billing-accounts row"),
        _billing_row_run,
    ),
    WorkspaceStepDef(
        "user_row",
        "user_repo.delete",
        LAYER_IDENTITY,
        lambda snap: (
            (True, f"{len(member_user_ids(snap))} users row(s)")
            if member_user_ids(snap)
            else (False, "no users rows for workspace")
        ),
        _user_rows_run,
    ),
    WorkspaceStepDef(
        "paperclip_agent_keys",
        "delete_owner_agent_key_secrets",
        LAYER_PAPERCLIP,
        _agent_keys_preview,
        _agent_keys_run,
    ),
    WorkspaceStepDef(
        "provisioning",
        "cancel_open_workflows",
        LAYER_RUNTIME,
        lambda snap: (
            (True, f"{len(snap.open_workflows)} open provisioning workflow(s) would be cancelled (never deprovisioned)")
            if snap.open_workflows
            else (False, "no open provisioning workflows")
        ),
        _provisioning_run,
    ),
    WorkspaceStepDef(
        "owner_secrets", "delete_owner_secrets", LAYER_SECRETS, _owner_secrets_preview, _owner_secrets_run
    ),
    WorkspaceStepDef("integrations", "delete_all_for_owner", LAYER_SECRETS, _integrations_preview, _integrations_run),
    WorkspaceStepDef(
        "oauth_tokens",
        "revoke_user_oauth",
        LAYER_SECRETS,
        lambda snap: (True, f"stored tokens for workspace + {len(member_user_ids(snap))} member(s)"),
        _oauth_run,
    ),
    WorkspaceStepDef(
        "agent_email_addresses", "delete_all_for_owner", LAYER_SECRETS, _agent_email_preview, _agent_email_run
    ),
    WorkspaceStepDef(
        "slack_installs",
        "delete_all_for_owner",
        LAYER_SECRETS,
        lambda snap: (True, ", ".join(r.get("team_id", "?") for r in snap.slack_rows) or "none"),
        _slack_run,
    ),
    WorkspaceStepDef("company_slug", "delete_for_owner", LAYER_IDENTITY, _slug_preview, _slug_run),
    WorkspaceStepDef(
        "clerk_user",
        "delete_user",
        LAYER_IDENTITY,
        lambda snap: (
            (True, f"{len(member_user_ids(snap))} member Clerk user(s)")
            if member_user_ids(snap)
            else (False, "no member Clerk users")
        ),
        _clerk_users_run,
    ),
    WorkspaceStepDef(
        "clerk_org",
        "delete_organization",
        LAYER_IDENTITY,
        lambda snap: (True, snap.org_id),
        _clerk_org_run,
    ),
)

#: (system, action) pairs in execution order — the parity surface the tests
#: assert against the user-keyed path's EXPECTED_ORDER.
WORKSPACE_STEP_ORDER: tuple[tuple[str, str], ...] = tuple((d.system, d.action) for d in WORKSPACE_STEP_REGISTRY)


def normalize_layers(layers: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """Validate + normalize a layer selection. None means the default (all but billing)."""
    if layers is None:
        return DEFAULT_LAYERS
    unknown = [layer for layer in layers if layer not in ALL_LAYERS]
    if unknown:
        raise WorkspaceWipeError(f"unknown wipe layer(s): {', '.join(unknown)} (known: {', '.join(ALL_LAYERS)})")
    return tuple(dict.fromkeys(layers))


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class WorkspaceWipeReport(WipeReport):
    """A ``WipeReport`` plus the workspace-wipe safety surface.

    ``fingerprint`` binds an execute to the preview it was confirmed from;
    ``layers`` records what was selected; ``billing_preserved`` is True when
    the billing layer was left off (the default).
    """

    fingerprint: str = ""
    layers: list[str] = field(default_factory=list)
    billing_preserved: bool = True

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["fingerprint"] = self.fingerprint
        base["layers"] = self.layers
        base["billing_preserved"] = self.billing_preserved
        return base


# ---------------------------------------------------------------------------
# preview_workspace_wipe — read-only
# ---------------------------------------------------------------------------


async def preview_workspace_wipe(
    org_id: str,
    *,
    layers: list[str] | tuple[str, ...] | None = None,
    include_billing: bool = False,
) -> WorkspaceWipeReport:
    """Read-only blast-radius enumeration for a workspace wipe.

    Calls NO delete method. Billing steps are shown ``skipped`` (preserved by
    default) unless ``include_billing`` — the override is confirmed at
    execute time, not here.
    """
    if not is_org_owner_id(org_id):
        raise WorkspaceWipeError(f"{org_id!r} is not a workspace (Clerk organization) id.")
    selected_layers = normalize_layers(layers)
    snap = await _read_workspace_snapshot(org_id)
    steps: list[WipeStep] = []
    # One loop over the registry, in order: unselected layers and the
    # preserved-by-default billing layer are shown as skipped (not omitted),
    # so the operator sees the whole blast radius including what is kept.
    for defn in WORKSPACE_STEP_REGISTRY:
        if defn.layer == LAYER_BILLING and (defn.layer not in selected_layers or not include_billing):
            # Billing is never deleted quietly: whether the layer was left off
            # the selection (the default) or included only for enumeration,
            # the preview says PRESERVED, not merely "not selected".
            steps.append(
                WipeStep(
                    system=defn.system,
                    action=defn.action,
                    status="skipped",
                    detail="PRESERVED by default — billing/Stripe moves only with include_billing + confirm_billing",
                )
            )
            continue
        if defn.layer not in selected_layers:
            _skip(steps, defn.system, defn.action, f"layer {defn.layer!r} not selected")
            continue
        exists, detail = defn.preview(snap)
        _preview_step(
            steps,
            system=defn.system,
            action=defn.action,
            exists=exists,
            detail_if_exists=detail,
            detail_if_absent=detail,
        )
    return WorkspaceWipeReport(
        owner_id=org_id,
        steps=steps,
        dry_run=True,
        fingerprint=snap.fingerprint,
        layers=list(selected_layers),
        billing_preserved=not include_billing,
    )


# ---------------------------------------------------------------------------
# wipe_workspace — the ordered teardown
# ---------------------------------------------------------------------------


async def wipe_workspace(
    org_id: str,
    *,
    actor_admin_id: str,
    confirm_org: str,
    layers: list[str] | tuple[str, ...] | None = None,
    include_billing: bool = False,
    confirm_billing: str | None = None,
    expected_fingerprint: str | None = None,
) -> WorkspaceWipeReport:
    """Execute the workspace teardown for the selected layers.

    Guards, in order, before anything is deleted: org shape, ``confirm_org``
    match (the typed workspace id must name THIS workspace), billing override
    (``include_billing`` + ``confirm_billing == "DELETE-BILLING"``), bootstrap
    (any member email on the protection list refuses the whole wipe), stale
    preview (fresh re-snapshot vs ``expected_fingerprint``). Clerk org delete
    runs last so nothing regrows.
    """
    if not is_org_owner_id(org_id):
        raise WorkspaceWipeError(f"{org_id!r} is not a workspace (Clerk organization) id.")
    if (confirm_org or "").strip() != org_id:
        raise WorkspaceConfirmMismatchError(
            f"confirm_org {confirm_org!r} does not match workspace {org_id!r}; refusing."
        )
    selected_layers = normalize_layers(layers)
    billing_armed = LAYER_BILLING in selected_layers and include_billing
    if LAYER_BILLING in selected_layers and not billing_armed:
        logger.warning(
            "workspace_wipe: org=%s billing layer selected without the explicit override — preserving",
            org_id,
        )
    if include_billing and confirm_billing != BILLING_CONFIRM_PHRASE:
        raise BillingPreservedError(f"billing override requires confirm_billing={BILLING_CONFIRM_PHRASE!r}; refusing.")

    snap = await _read_workspace_snapshot(org_id)
    if expected_fingerprint and expected_fingerprint != snap.fingerprint:
        raise StaleWorkspacePreviewError(expected_fingerprint, snap.fingerprint)

    # Bootstrap guard up front, before any teardown: any member carrying a
    # bootstrap admin identity refuses the whole wipe (deleting it breaks the
    # PaperclipAdminClient session every provision depends on).
    for uid, email in snap.member_emails.items():
        if email:
            _refuse_bootstrap_account(uid, [email])
    # The org id itself is never a bootstrap identity, but a member row whose
    # email lookup missed ("" above) still reaches delete_paperclip_user's own
    # per-row guard — defense in depth, not a silent pass.
    _assert_no_bootstrap_members(snap)

    logger.info(
        "workspace_wipe.wipe_workspace: starting for org=%s actor=%s layers=%s billing_armed=%s",
        org_id,
        actor_admin_id,
        ",".join(selected_layers),
        billing_armed,
    )
    steps: list[WipeStep] = []
    # One loop over the registry, in order — the same enumeration the preview
    # builds, so the two cannot diverge. Unselected layers (and preserved
    # billing) are recorded skipped, not omitted.
    for defn in WORKSPACE_STEP_REGISTRY:
        if defn.layer not in selected_layers:
            _skip(steps, defn.system, defn.action, f"layer {defn.layer!r} not selected")
        elif defn.layer == LAYER_BILLING and not billing_armed:
            _skip(
                steps,
                defn.system,
                defn.action,
                "PRESERVED by default — billing/Stripe moves only with include_billing + confirm_billing",
            )
        else:
            await _step(steps, system=defn.system, action=defn.action, fn=_bind(defn, snap))
    logger.warning(
        "workspace_wipe.wipe_workspace: billing %s for org=%s",
        "ARMED (money will move)" if billing_armed else "preserved by default",
        org_id,
    )
    logger.info(
        "workspace_wipe.wipe_workspace: finished for org=%s actor=%s ok=%d skipped=%d failed=%d refused=%d",
        org_id,
        actor_admin_id,
        sum(1 for s in steps if s.status == "ok"),
        sum(1 for s in steps if s.status == "skipped"),
        sum(1 for s in steps if s.status == "failed"),
        sum(1 for s in steps if s.status == "refused"),
    )
    return WorkspaceWipeReport(
        owner_id=org_id,
        steps=steps,
        dry_run=False,
        fingerprint=snap.fingerprint,
        layers=list(selected_layers),
        billing_preserved=not billing_armed,
    )


def _assert_no_bootstrap_members(snap: WorkspaceSnapshot) -> None:
    """Refuse when any member's Paperclip-row email is a bootstrap identity.

    ``member_emails`` comes from the users rows (unreliable webhook write, may
    be absent); the Paperclip row carries its own ``paperclip_email`` copy —
    check both so a missing users-row email cannot sail through.
    """
    for row in snap.member_rows:
        for email in (row.get("paperclip_email") or "",):
            if email and email.lower() in {b.lower() for b in BOOTSTRAP_EMAILS}:
                raise BootstrapAccountProtectedError(row.get("user_id") or snap.org_id)


def _bind(defn: WorkspaceStepDef, snap: WorkspaceSnapshot) -> Callable[[], Awaitable[str]]:
    async def _fn() -> str:
        return await defn.run(snap)

    return _fn
