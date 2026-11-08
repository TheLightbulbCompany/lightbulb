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
usage counters), ``paperclip`` (company, per-member Aurora users, the
workspace row + deleted members' identity rows + pending briefs, agent-key
secrets), ``messaging``
(room partitions + owner partition), ``secrets`` (owner secrets, OpenRouter
key, integrations, OAuth tokens, agent email, slack installs), ``identity``
(per-member users rows, slug, join link, per-member Clerk users, Clerk org —
ALWAYS LAST), ``billing`` (Metronome + fast-gate row, Stripe customer +
company-stripe deauthorize, billing row).

Members are the union of the org's Clerk memberships and the workspace row's
``member_user_ids``, read ONCE into the snapshot before any delete, so a re-run
after a partial failure still finds everyone (and the Clerk org is kept while
any member delete failed). A member who also belongs to another org keeps their
account AND their Paperclip identity row — only this org's membership goes,
with the org — unless ``delete_shared_members``. The vendor org
(``VENDOR_ORG_ID``) and an org the acting admin belongs to are refused. The
managed OpenRouter key is retired (entitlement_teardown's path) and its row +
secrets deleted only once the settlement worker has disabled it; until then
that step fails and asks for a re-run.

The Paperclip company deleted is the one named on the workspace row — one
workspace = one company, keyed by org — and the workspace row goes with it. A
read failure of that row stops preview/execute with nothing deleted.

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

import boto3

from core.auth import is_org_owner_id
from core.billing.bedrock_gate_tokens import delete_gate_token_rows, owner_gate_token_hashes
from core.billing.metronome_fast_gate import MetronomeFastGate
from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.repositories import (
    agent_email_repo,
    billing_repo,
    company_slug_repo,
    container_repo,
    integrations_repo,
    messaging_repo,
    openrouter_key_repo,
    provisioning_workflow_repo,
    slack_installs_repo,
    usage_repo,
    user_repo,
    workspace_join_repo,
)
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import clerk_admin, company_stripe, message_search, voice_onboarding
from core.services.account_wipe import (
    release_text_lines,
    text_release_note,
    WipeReport,
    WipeStep,
    _archive_metronome_customer,
    _delete_paperclip_company_call,
    _NO_PAPERCLIP_ROW,
    _preview_step,
    _runtime_detail,
    _refuse_bootstrap_account,
    _skip,
    _step,
    cancel_open_workflows,
)
from core.services.billing_service import delete_stripe_customer
from core.services.hermes_provisioning import delete_workspace_runtime
from core.services.voice_calls import wipe_owner_voice
from core.services.oauth_service import revoke_user_oauth
from core.services.owner_secret_sweep import (
    _list_names_with_prefix,
    delete_owner_agent_key_secrets,
    delete_owner_secrets,
    list_owner_agent_key_secret_names,
    list_owner_secret_names,
)
from core.services.paperclip_user_delete import delete_paperclip_user

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


class OtherMembersRemainError(WorkspaceWipeError):
    """Self-serve: other Clerk members remain, so the caller leaves and the workspace stays."""


class SelfServeBlockedError(WorkspaceWipeError):
    """Self-serve refusal a retry cannot clear; the message is shown to the user."""


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
    #: Every person in the workspace: the union of Clerk memberships and the
    #: workspace row's ``member_user_ids``, read ONCE before any delete. Each carries user_id,
    #: email, emails (every known address, for the bootstrap guard),
    #: paperclip_user_id and other_orgs — so no step re-derives its member list
    #: from rows an earlier step deletes, and a re-run still finds them via Clerk.
    members: list[dict]
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
    join_link: dict | None = None
    openrouter_row: dict | None = None
    # The options this snapshot was taken FOR — part of the fingerprint, so a
    # preview binds exactly what executes.
    layers: tuple[str, ...] = ()
    include_billing: bool = False
    delete_shared_members: bool = False
    fingerprint: str = ""
    #: Execute-time: member ids (and "paperclip_company") whose delete failed.
    #: Steps that would destroy the handle a re-run needs check this first.
    unfinished: set[str] = field(default_factory=set)


def member_user_ids(snapshot: WorkspaceSnapshot) -> list[str]:
    """Every member user id, snapshot order."""
    return [m["user_id"] for m in snapshot.members]


def deletable_member_ids(snapshot: WorkspaceSnapshot) -> list[str]:
    """Members whose ACCOUNT this wipe deletes. A member who also belongs to
    another org keeps their account (only this org's membership goes, with the
    org itself) unless ``delete_shared_members`` opted in."""
    return [m["user_id"] for m in snapshot.members if snapshot.delete_shared_members or not m["other_orgs"]]


async def _read_members(org_id: str, repo: PaperclipRepo, joined: set[str]) -> list[dict]:
    clerk = {
        m["user_id"]: m for m in await clerk_admin.list_organization_members(org_id, strict=True) if m.get("user_id")
    }
    members: list[dict] = []
    for uid in dict.fromkeys([*sorted(joined), *clerk]):
        identity = await repo.get_identity(uid)
        try:
            user_row = await user_repo.get(uid)
        except Exception:  # noqa: BLE001 -- best-effort; the other two sources still apply
            user_row = None
        emails = [
            e
            for e in dict.fromkeys(
                [
                    identity.paperclip_email if identity else None,
                    (clerk.get(uid) or {}).get("email"),
                    (user_row or {}).get("email"),
                ]
            )
            if e
        ]
        # strict: "no other orgs" decides an account delete, so a Clerk blip
        # must fail the snapshot rather than read as "sole workspace".
        orgs = await clerk_admin.list_user_organizations(uid, strict=True)
        members.append(
            {
                "user_id": uid,
                "email": emails[0] if emails else "",
                "emails": emails,
                "paperclip_user_id": identity.paperclip_user_id if identity else "",
                "other_orgs": [o["id"] for o in orgs if o.get("id") and o["id"] != org_id],
                "in_clerk": uid in clerk,
            }
        )
    return members


async def _read_workspace_snapshot(
    org_id: str,
    *,
    layers: tuple[str, ...] = DEFAULT_LAYERS,
    include_billing: bool = False,
    delete_shared_members: bool = False,
) -> WorkspaceSnapshot:
    container = await container_repo.get_by_owner_id(org_id)
    billing = await billing_repo.get_by_owner_id(org_id)
    paperclip_repo = PaperclipRepo(table_name="paperclip-companies")
    # Before any step: a read failure here stops preview/execute with nothing deleted.
    workspace = await paperclip_repo.get_workspace(org_id)
    # ponytail: once the workspace row is gone (a prior half-wipe), the company
    # id is unrecoverable from DDB and the Aurora company survives; there is no
    # org->company lookup against Paperclip itself to fall back on.
    company_id = (workspace.company_id or None) if workspace is not None else None
    members = await _read_members(org_id, paperclip_repo, workspace.member_user_ids if workspace else set())
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
        members=members,
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
        join_link=await workspace_join_repo.get_link(org_id),
        openrouter_row=await openrouter_key_repo.get(org_id),
        layers=layers,
        include_billing=include_billing,
        delete_shared_members=delete_shared_members,
    )
    snapshot.fingerprint = snapshot_fingerprint(snapshot)
    return snapshot


def snapshot_fingerprint(snapshot: WorkspaceSnapshot) -> str:
    """Stable hash of the snapshot's SHAPE (ids, counts, names — no values)
    plus the options it was taken for (layers, billing, shared-member opt-in).

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
        "members": sorted([m["user_id"], sorted(m["other_orgs"])] for m in snapshot.members),
        "room_ids": sorted(snapshot.room_ids),
        "secret_names": sorted(snapshot.secret_names),
        "agent_key_names": sorted(snapshot.agent_key_names),
        "integrations": sorted(r.get("provider", "?") for r in snapshot.integration_rows),
        "agent_emails": sorted(r.get("address", "?") for r in snapshot.email_rows),
        "slack_teams": sorted(r.get("team_id", "?") for r in snapshot.slack_rows),
        "slug": (snapshot.slug_row or {}).get("slug", ""),
        "open_workflows": sorted(r.get("workflow_id", "?") for r in snapshot.open_workflows),
        "gate_hash_count": len(snapshot.gate_hashes),
        "join_link": snapshot.join_link is not None,
        "openrouter_state": (snapshot.openrouter_row or {}).get("state", ""),
        "layers": sorted(snapshot.layers),
        "include_billing": snapshot.include_billing,
        "delete_shared_members": snapshot.delete_shared_members,
    }
    return hashlib.sha256(json.dumps(canonical, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# User -> org resolution (explicit confirmation required on ambiguity)
# ---------------------------------------------------------------------------


async def resolve_workspace_org(user_id: str, confirm_org: str | None = None) -> str:
    """Resolve which workspace ``user_id``'s wipe means.

    Source: the user's Clerk memberships. When the user sits in several
    workspaces there is no safe guess: the caller must pass ``confirm_org`` naming the intended org
    explicitly, else ``AmbiguousWorkspaceError``. A ``confirm_org`` that
    matches NO known membership raises ``WorkspaceConfirmMismatchError``.
    """
    candidates: list[str] = []
    try:
        memberships = await clerk_admin.list_user_organizations(user_id)
    except Exception:  # noqa: BLE001 -- resolution must not fail closed on a Clerk blip
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
    try:
        await _delete_paperclip_company_call(snap.company_id)
    except Exception:
        snap.unfinished.add("paperclip_company")
        raise
    return f"company_id={snap.company_id}"


def _aurora_targets(snap: WorkspaceSnapshot) -> list[dict]:
    deletable = set(deletable_member_ids(snap))
    return [m for m in snap.members if m["user_id"] in deletable and m["paperclip_user_id"]]


def _aurora_users_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    ids = [m["paperclip_user_id"] for m in _aurora_targets(snap)]
    if not ids:
        return False, _NO_PAPERCLIP_ROW
    return True, f"{len(ids)} Better Auth user(s): {', '.join(ids)}"


async def _aurora_users_run(snap: WorkspaceSnapshot) -> str:
    env = settings.ENVIRONMENT or "dev"
    failures: list[str] = []
    deleted = 0
    for m in _aurora_targets(snap):
        try:
            await delete_paperclip_user(paperclip_user_id=m["paperclip_user_id"], email=m["email"], env=env)
            deleted += 1
        except Exception as e:  # noqa: BLE001 -- per-member partial failure, aggregated below
            # Keep this member's paperclip row: it is the only record of the
            # paperclip_user_id a re-run needs.
            snap.unfinished.add(m["user_id"])
            failures.append(f"{m['user_id']}: {e}")
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
    try:  # end live voice calls before the ledger is archived; never abort the wipe
        await wipe_owner_voice(snap.org_id)
    except Exception:  # noqa: BLE001
        logger.warning("voice wipe failed owner=%s", snap.org_id, exc_info=True)
    # With no snapshot row, call anyway: a provision racing the wipe may have created
    # the row after the snapshot. The seam is idempotent on a missing row (returns None).
    report: dict = {}
    row_at_death = await delete_workspace_runtime(snap.org_id, delete_profiles=True, report=report)
    if snap.container is None and row_at_death is None:
        return "no containers row"
    detail = _runtime_detail(report)
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


async def _search_run(snap: WorkspaceSnapshot) -> str:
    return await message_search.delete_owner_index(snap.org_id)


async def _messaging_run(snap: WorkspaceSnapshot) -> str:
    # Fresh re-list: a message racing the wipe creates rooms the snapshot
    # predates. Keep the owner catalog until every room partition is gone so a
    # partial failure retries from a complete inventory.
    room_ids = await messaging_repo.list_owner_room_ids(snap.org_id)
    deleted = 0
    foreign: list[str] = []
    owned = 0
    for room_id in room_ids:
        meta = await messaging_repo.get_room(room_id)
        if meta is not None and meta.get("ownerId") != snap.org_id:
            # A room another owner owns (e.g. a shared channel) is not ours to
            # delete; only this owner's catalog entry for it goes, with the
            # owner partition below.
            foreign.append(room_id)
            continue
        deleted += await messaging_repo.delete_room_partition(room_id)
        owned += 1
    deleted += bool(await voice_onboarding.release_number(snap.org_id))  # the PHONE# line row, before the page goes
    deleted += await messaging_repo.delete_owner_partition(snap.org_id)
    detail = f"deleted {owned} room partition(s) and {deleted} messaging row(s)"
    if foreign:
        detail += f"; skipped {len(foreign)} room(s) owned elsewhere: {', '.join(foreign)}"
    return detail


def _paperclip_rows_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    # The workspace row and pending briefs are always attempted (idempotent).
    return True, (
        f"workspace row + {len(deletable_member_ids(snap))} member identity row(s) + org and member "
        "pending_brief items (always attempted)"
    )


async def _paperclip_rows_run(snap: WorkspaceSnapshot) -> str:
    if "paperclip_company" in snap.unfinished:
        raise RuntimeError("kept paperclip-companies rows: company delete failed and they carry its id; re-run")
    repo = PaperclipRepo(table_name="paperclip-companies")
    deleted: list[str] = []
    kept: list[str] = []
    await repo.delete(snap.org_id)
    deleted.append(f"{snap.org_id}: workspace row")
    # Identity rows only for accounts this wipe deletes: a member who keeps
    # their account is still in other workspaces' companies under it.
    for uid in deletable_member_ids(snap):
        if uid in snap.unfinished:
            kept.append(uid)
            continue
        await repo.delete(uid)
        deleted.append(f"{uid}: identity row")
    for owner in [snap.org_id, *deletable_member_ids(snap)]:
        if await repo.delete_pending_brief(owner):
            deleted.append(f"{owner}: pending_brief")
    detail = ("deleted " + "; ".join(deleted)) if deleted else "no paperclip-companies or pending_brief rows"
    if kept:
        detail += f"; kept row(s) for re-run (Better Auth delete failed): {', '.join(kept)}"
    return detail


async def _billing_row_run(snap: WorkspaceSnapshot) -> str:
    if snap.billing is None:
        # Unconditional attempt: the snapshot predates the Stripe steps and a
        # billing row created mid-wipe must not survive. Delete is idempotent.
        await billing_repo.delete(snap.org_id)
        return "no billing-accounts row"
    await billing_repo.delete(snap.org_id)
    return "billing-accounts row deleted"


async def _user_rows_run(snap: WorkspaceSnapshot) -> str:
    members = deletable_member_ids(snap)
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
    for uid in deletable_member_ids(snap):
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
    for uid in deletable_member_ids(snap):
        names += await delete_owner_secrets(uid)
    return f"deleted {len(names)} owner secret(s) (BYOK keys, integration creds, OAuth apps)"


def _integrations_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.integration_rows:
        return False, "no integration rows"
    return True, f"{len(snap.integration_rows)} integration row(s)"


async def _integrations_run(snap: WorkspaceSnapshot) -> str:
    providers = await integrations_repo.delete_all_for_owner(snap.org_id)
    for uid in deletable_member_ids(snap):
        providers += await integrations_repo.delete_all_for_owner(uid)
    return f"deleted {len(providers)} integration row(s): {', '.join(providers) or 'none'}"


async def _oauth_run(snap: WorkspaceSnapshot) -> str:
    targets = [snap.org_id, *deletable_member_ids(snap)]
    for uid in targets:
        await revoke_user_oauth(user_id=uid)
    return f"stored chatgpt oauth tokens deleted for {len(targets)} principal(s) (grant NOT revoked upstream)"


def _agent_email_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.email_rows:
        return False, "no agent email addresses"
    addrs = ", ".join(r.get("address", "?") for r in snap.email_rows)
    return True, f"{len(snap.email_rows)} address(es): {addrs}"


# ponytail: agent-mail-inbox rows for these addresses are left to their 30-day
# TTL — the repo has no delete and list_messages caps at 50 per call. Add a
# delete-by-address to agent_mail_inbox_repo if TTL expiry is not enough.
async def _agent_email_run(snap: WorkspaceSnapshot) -> str:
    released, failed = await release_text_lines(snap.email_rows)
    addresses = await agent_email_repo.delete_all_for_owner(snap.org_id)
    return f"deleted {len(addresses)} agent email address(es): {', '.join(addresses) or 'none'}" + text_release_note(
        released, failed
    )


async def _slack_run(snap: WorkspaceSnapshot) -> str:
    teams = await slack_installs_repo.delete_all_for_owner(snap.org_id)
    return f"deleted {len(teams)} slack install(s): {', '.join(teams) or 'none'}"


def _slug_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if snap.slug_row is None:
        return False, "no slug reservation"
    return True, f"slug {(snap.slug_row or {}).get('slug')}"


async def _slug_run(snap: WorkspaceSnapshot) -> str:
    slug = await company_slug_repo.delete_for_owner(snap.org_id)
    for uid in deletable_member_ids(snap):
        member_slug = await company_slug_repo.delete_for_owner(uid)
        slug = slug or member_slug
    return f"released slug {slug}" if slug else "no slug reservation"


def _openrouter_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if snap.openrouter_row is None:
        return False, "no managed OpenRouter key"
    return True, f"managed OpenRouter key (state={snap.openrouter_row.get('state')}): retire, then row + secret"


async def _delete_openrouter_key(owner_id: str) -> None:
    """Drop the key row and every secret under the owner's OpenRouter prefix
    (earlier hashes included). Only called once the key is ``disabled``."""
    prefix = f"lightbulb/{settings.ENVIRONMENT or 'dev'}/openrouter/owners/{owner_id}/"

    def _delete() -> None:
        sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
        for name in _list_names_with_prefix(sm, prefix):
            sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        get_table("openrouter-keys").delete_item(Key={"owner_id": owner_id})

    await run_in_thread(_delete)


async def _openrouter_run(snap: WorkspaceSnapshot) -> str:
    row = await openrouter_key_repo.get(snap.org_id)
    if row is None:
        return "no managed OpenRouter key"
    if row.get("state") != "disabled":
        # The same retirement entitlement_teardown uses. The settlement worker
        # drains pending generations with the key's secret, then disables it
        # upstream — deleting row + secret now would strand a LIVE upstream key.
        # Retiring is terminal for the wipe: the gate refuses a non-active key and no runtime
        # ever holds it, so nothing can spend it. The worker's own cleanup finishes it.
        await openrouter_key_repo.mark_retiring(snap.org_id)
        return f"OpenRouter key {row.get('state')} -> retiring; the settlement worker disables and cleans it up"
    await _delete_openrouter_key(snap.org_id)
    return "disabled OpenRouter key row + secrets deleted"


async def _join_link_run(snap: WorkspaceSnapshot) -> str:
    await workspace_join_repo.delete_link(snap.org_id)
    return "workspace join link deleted"


async def _fast_gate_run(snap: WorkspaceSnapshot) -> str:
    await MetronomeFastGate().delete_row(snap.org_id)
    return "metronome fast-gate row deleted"


def member_plan(snap: WorkspaceSnapshot) -> list[dict]:
    """Per person: delete the account, or remove only this org's membership."""
    deletable = set(deletable_member_ids(snap))
    return [
        {
            "user_id": m["user_id"],
            "email": m["email"],
            "action": "delete_account" if m["user_id"] in deletable else "remove_membership",
            "other_orgs": m["other_orgs"],
        }
        for m in snap.members
    ]


def _clerk_users_preview(snap: WorkspaceSnapshot) -> tuple[bool, str]:
    if not snap.members:
        return False, "no member Clerk users"
    lines = [
        f"{p['user_id']}: delete account"
        if p["action"] == "delete_account"
        else f"{p['user_id']}: remove membership only (also in {', '.join(p['other_orgs'])})"
        for p in member_plan(snap)
    ]
    return True, "; ".join(lines)


async def _clerk_users_run(snap: WorkspaceSnapshot) -> str:
    members = deletable_member_ids(snap)
    failures: list[str] = []
    deleted = 0
    for uid in members:
        try:
            await clerk_admin.delete_user(uid)
            deleted += 1
        except Exception as e:  # noqa: BLE001 -- per-member partial failure
            snap.unfinished.add(uid)
            failures.append(f"{uid}: {e}")
    if failures:
        raise RuntimeError(f"deleted {deleted} Clerk user(s); failures: {'; '.join(failures)}")
    kept = len(snap.members) - len(members)
    detail = f"deleted {deleted} member Clerk user(s)" if deleted else "no member Clerk users deleted"
    return detail + (f"; kept {kept} shared member account(s) (membership goes with the org)" if kept else "")


async def _clerk_org_run(snap: WorkspaceSnapshot) -> str:
    # ALWAYS LAST: leaving the org alive lets everything regrow on the next
    # /container/status poll. But a failed member delete keeps it: the org's
    # Clerk member list is what lets a re-run find that member again.
    if snap.unfinished - {"paperclip_company"}:
        raise RuntimeError(
            f"kept Clerk org: member delete(s) failed for {', '.join(sorted(snap.unfinished))}; re-run to finish"
        )
    await clerk_admin.delete_organization(snap.org_id)
    return snap.org_id


# Steps whose targets include per-member keys; a failure in any of them must
# hold back the Clerk org delete (see wipe_workspace).
_MEMBER_KEYED_SYSTEMS = frozenset(
    {"paperclip_agent_keys", "owner_secrets", "integrations", "oauth_tokens", "company_slug", "user_row"}
)


# A failed money step (subscription/usage billing may still be live) holds
# back every later step that would destroy the handle a re-run needs.
_MONEY_STEPS = frozenset({"stripe", "metronome"})
_HELD_BY_MONEY = frozenset({"billing_row", "clerk_user", "clerk_org"})


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
    WorkspaceStepDef(
        "metronome_fast_gate",
        "delete_row",
        LAYER_BILLING,
        lambda snap: (True, "metronome fast-gate row"),
        _fast_gate_run,
    ),
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
        "search",
        "drop_owner_index",
        LAYER_MESSAGING,
        lambda snap: (True, "the workspace's message search index"),
        _search_run,
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
            (True, f"{len(deletable_member_ids(snap))} users row(s)")
            if deletable_member_ids(snap)
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
    WorkspaceStepDef("openrouter_key", "retire_and_delete", LAYER_SECRETS, _openrouter_preview, _openrouter_run),
    WorkspaceStepDef("integrations", "delete_all_for_owner", LAYER_SECRETS, _integrations_preview, _integrations_run),
    WorkspaceStepDef(
        "oauth_tokens",
        "revoke_user_oauth",
        LAYER_SECRETS,
        lambda snap: (True, f"stored tokens for workspace + {len(deletable_member_ids(snap))} member(s)"),
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
        "workspace_join_link",
        "delete_link",
        LAYER_IDENTITY,
        lambda snap: (True, "workspace join link") if snap.join_link else (False, "no workspace join link"),
        _join_link_run,
    ),
    WorkspaceStepDef("clerk_user", "delete_user", LAYER_IDENTITY, _clerk_users_preview, _clerk_users_run),
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
    #: Per person: ``action`` is "delete_account" or "remove_membership".
    members: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        base = super().to_dict()
        base["fingerprint"] = self.fingerprint
        base["layers"] = self.layers
        base["billing_preserved"] = self.billing_preserved
        base["members"] = self.members
        return base


# ---------------------------------------------------------------------------
# preview_workspace_wipe — read-only
# ---------------------------------------------------------------------------


async def preview_workspace_wipe(
    org_id: str,
    *,
    layers: list[str] | tuple[str, ...] | None = None,
    include_billing: bool = False,
    delete_shared_members: bool = False,
) -> WorkspaceWipeReport:
    """Read-only blast-radius enumeration for a workspace wipe.

    Calls NO delete method. Billing steps are shown ``skipped`` (preserved by
    default) unless ``include_billing`` — the override is confirmed at
    execute time, not here. The fingerprint binds these exact options.
    """
    _refuse_org(org_id)
    selected_layers = normalize_layers(layers)
    snap = await _read_workspace_snapshot(
        org_id, layers=selected_layers, include_billing=include_billing, delete_shared_members=delete_shared_members
    )
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
        members=member_plan(snap),
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
    delete_shared_members: bool = False,
    self_serve: bool = False,
) -> WorkspaceWipeReport:
    """Execute the workspace teardown for the selected layers.

    ``self_serve``: the actor is deleting their own account, so instead of
    refusing a member actor it REQUIRES the actor to be the only member.

    Guards, in order, before anything is deleted: org shape, ``confirm_org``
    match (the typed workspace id must name THIS workspace), billing override
    (``include_billing`` + ``confirm_billing == "DELETE-BILLING"``), bootstrap
    (any member email on the protection list refuses the whole wipe), stale
    preview (fresh re-snapshot vs ``expected_fingerprint``, which is REQUIRED —
    an empty one would skip the drift check), acting admin not a member.
    Clerk org delete runs last so nothing regrows.
    """
    _refuse_org(org_id)
    if not (expected_fingerprint or "").strip():
        raise WorkspaceWipeError("preview fingerprint required; run the preview and confirm it.")
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

    snap = await _read_workspace_snapshot(
        org_id, layers=selected_layers, include_billing=include_billing, delete_shared_members=delete_shared_members
    )
    if expected_fingerprint != snap.fingerprint:
        raise StaleWorkspacePreviewError(expected_fingerprint, snap.fingerprint)
    if self_serve:
        if set(member_user_ids(snap)) != {actor_admin_id}:
            raise WorkspaceWipeError(f"{actor_admin_id} is not the sole member of {org_id}; refusing to wipe it.")
    elif actor_admin_id in member_user_ids(snap):
        raise WorkspaceWipeError(f"acting admin {actor_admin_id} is a member of {org_id}; refusing to wipe it.")

    # Bootstrap guard up front, before any teardown: any member carrying a
    # bootstrap admin identity (under ANY known email: paperclip row, Clerk,
    # users row) refuses the whole wipe (deleting it breaks the
    # PaperclipAdminClient session every provision depends on).
    for m in snap.members:
        if m["emails"]:
            _refuse_bootstrap_account(m["user_id"], m["emails"])

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
            if defn.system == "clerk_org":
                # Member-keyed data is found again only through the org's Clerk
                # member list: any failure there keeps the org for the re-run.
                snap.unfinished |= {
                    f"step:{s.system}" for s in steps if s.status == "failed" and s.system in _MEMBER_KEYED_SYSTEMS
                }
            unpaid = sorted(u for u in snap.unfinished if u in _MONEY_STEPS)
            if unpaid and defn.system in _HELD_BY_MONEY:
                # A subscription may still be live: keep the billing row and the
                # Clerk identities a re-run needs to find and cancel it.
                steps.append(
                    WipeStep(
                        defn.system,
                        defn.action,
                        "failed",
                        error=f"held back: {', '.join(unpaid)} failed (subscription may still be live); re-run to finish",
                    )
                )
                continue
            await _step(steps, system=defn.system, action=defn.action, fn=_bind(defn, snap))
            if defn.system in _MONEY_STEPS and steps[-1].status == "failed":
                snap.unfinished.add(defn.system)
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
        members=member_plan(snap),
    )


async def wipe_own_workspace(org_id: str, *, user_id: str) -> WorkspaceWipeReport:
    """Self-serve account deletion by a workspace's SOLE member (App Store 5.1.1(v)).

    The snapshot is the one member source: ``OtherMembersRemainError`` when
    another Clerk member remains (the caller leaves instead), and
    ``SelfServeBlockedError`` when only a leftover row names someone else (a
    retry cannot clear that). Both raise before anything is deleted.

    Then the join link goes FIRST, so nobody can join between the snapshot
    and the teardown, and the full wipe runs: every layer, billing armed
    (``delete_stripe_customer`` cancels the subscription before deleting the
    customer), the caller's account deleted even if they are in other orgs.
    Every ``wipe_workspace`` guard still applies (bootstrap, vendor org,
    fingerprint); the org row names exactly one company, which is the only
    one the wipe deletes (#3097).
    """
    _refuse_org(org_id)
    options = {"layers": ALL_LAYERS, "include_billing": True, "delete_shared_members": True}
    snap = await _read_workspace_snapshot(org_id, **options)
    others = [m for m in snap.members if m["user_id"] != user_id]
    if any(m["in_clerk"] for m in others):
        raise OtherMembersRemainError(f"{org_id} has other members")
    if others:
        raise SelfServeBlockedError(
            "This workspace still has records for people who are no longer members, so it can't be "
            f"deleted in-app. Retrying won't fix this — contact {settings.EMAIL_SUPPORT_ADDRESS}."
        )
    await workspace_join_repo.delete_link(org_id)
    preview = await preview_workspace_wipe(org_id, **options)
    return await wipe_workspace(
        org_id,
        actor_admin_id=user_id,
        confirm_org=org_id,
        confirm_billing=BILLING_CONFIRM_PHRASE,
        expected_fingerprint=preview.fingerprint,
        self_serve=True,
        **options,
    )


def _refuse_org(org_id: str) -> None:
    if not is_org_owner_id(org_id):
        raise WorkspaceWipeError(f"{org_id!r} is not a workspace (Clerk organization) id.")
    if settings.VENDOR_ORG_ID and org_id == settings.VENDOR_ORG_ID:
        raise WorkspaceWipeError(f"{org_id} is the vendor (Lightbulb) org; refusing.")


def _bind(defn: WorkspaceStepDef, snap: WorkspaceSnapshot) -> Callable[[], Awaitable[str]]:
    async def _fn() -> str:
        return await defn.run(snap)

    return _fn
