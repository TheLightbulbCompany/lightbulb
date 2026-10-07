"""DynamoDB repository for paperclip-companies table.

PK: user_id (S)
Maps Isol8 user -> Paperclip Better Auth account + the org-shared
Paperclip company. Each row is per-USER (not per-org); members of the
same Isol8 org share the same ``company_id`` value across multiple
rows.

Async repository following the same pattern as peer repos in
``core/repositories/`` (channel_link_repo, usage_repo, etc.): every
DynamoDB call is wrapped in ``run_in_thread`` and exposed as ``async def``
so future async callers (paperclip_provisioning, cleanup cron) can
``await`` directly without ``asyncio.to_thread`` at each call site.

**Multi-member org auth model.**

Paperclip's REST API has no per-user board-API-key minting endpoint
(it's only mintable via the CLI auth challenge flow). The right design
is therefore:

* One Paperclip company per Isol8 *org* (Clerk org). All Isol8 users
  belonging to the same org share the same ``company_id`` value.
* One Better Auth account per Isol8 *user*, identified by
  ``paperclip_user_id`` and authenticated with a per-user random
  password (Fernet-encrypted at rest as ``paperclip_password_encrypted``).
* Members are added to the shared company via Paperclip's
  server-side invite flow:
  ``signUp → admin createInvite → user acceptInvite → admin approveJoinRequest``.
* The OpenClaw service token (``service_token_encrypted``) remains
  per-user — it's how the seeded Paperclip agent reaches the user's
  own OpenClaw container.

To efficiently find an existing company_id when a new member joins,
``get_org_company_id`` queries the ``by-org-id`` GSI (PK: org_id).
For v1 we accept that the GSI projection is small (just company_id +
keys) since member-join is rare relative to read-by-user lookups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional
from uuid import uuid4

from boto3.dynamodb.conditions import Key

from core.auth import is_org_owner_id
from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)


def _broadcast_owner_key(*, user_id: str, org_id: str | None) -> str:
    """The id an owner's frontend WS tabs register under -- the value
    ``publish_status_changed`` / ``broadcast_to_owner`` must key on.

    For an org that is the Clerk ``org_id`` (``websocket_chat`` registers org
    members under ``owner_id = x_org_id or x_user_id``); for a personal owner
    it is the ``user_id``. This is NOT the paperclip-companies row PK: that PK
    is the row's ``user_id``, which for an org is the CREATOR's personal Clerk
    id (see ``resolve_owner_row``) -- an id no org tab is registered under, so
    a nudge keyed on it silently misses every org tab. Guarded by
    ``is_org_owner_id`` so a personal row whose ``org_id`` is empty/echoes the
    user_id still resolves to the user_id.
    """
    return org_id if (org_id and is_org_owner_id(org_id)) else user_id


class FunnelClaimResult(str, Enum):
    """What ``claim_funnel_submission`` decided, for the caller to act on.

    Replaces the old ``OnboardingClaimResult`` (the ``__onboarded__``
    sentinel's claim/steal vocabulary). The funnel claim is now a real,
    generation-fenced attribute transition on the owner row
    (``funnel_state: not_started -> submitted``)
    instead of a separate synthetic-key claim item, so there is no
    stale-steal timing to reason about: DynamoDB's own conditional write
    serializes concurrent submitters, and a losing caller is deterministic —
    it split into exactly the two cases below by re-reading the row.
    """

    WON = "won"
    """This caller's write landed: funnel_state flipped not_started ->
    submitted under the caller's ``onboarding_generation``. Proceed to
    seed."""

    ALREADY_SUBMITTED = "already_submitted"
    """The row is still on the SAME ``onboarding_generation`` the caller
    read, but ``funnel_state`` was no longer ``not_started`` -- someone
    else's submission already won (or completed). Durable, not transient:
    report ``already_submitted`` to the caller."""

    STALE_GENERATION = "stale_generation"
    """The row's ``onboarding_generation`` no longer matches what the caller
    fenced on (or the row vanished) -- per INV-4 this is a silent no-op from
    the caller's point of view. The caller was operating on a superseded
    view of the company (e.g. a reprovision minted a new generation
    underneath it); it should re-resolve the owner row and retry, not treat
    this as a duplicate submit."""


PaperclipCompanyStatus = Literal["seeding", "provisioning", "active", "failed", "disabled"]
FunnelState = Literal["not_started", "submitted", "complete"]


# Reserved user_id namespace for per-org "company create" claim sentinels.
# These items DELIBERATELY omit org_id / status / scheduled_purge_at so they
# never appear in the by-org-id or by-status-purge-at GSIs (DynamoDB only
# indexes items that carry the index's key attributes). They are reached only
# by direct get/put on this synthetic PK, which — unlike the eventually
# consistent by-org-id GSI used by ``find_org_owner`` — is strongly consistent,
# so it can serialize company creation across backend instances.
_ORG_CREATE_CLAIM_PREFIX = "__org_create_claim__:"


def _org_create_claim_key(org_id: str) -> str:
    return f"{_ORG_CREATE_CLAIM_PREFIX}{org_id}"


# Reserved user_id namespace for per-OWNER "the user answered the funnel, but
# their workspace wasn't ready to receive it yet" items. Same synthetic-PK
# construction as the org-create claim above, and for the same two reasons:
# reached only by direct get/put (strongly consistent, unlike the by-org-id
# GSI), and carrying none of the GSI key attributes so it never surfaces in a
# by-org-id or by-status-purge-at query as if it were a company row.
#
# Keyed by OWNER id (org_id for an org, user_id for a personal owner) rather
# than by the submitting member: onboarding is company-scoped, and the owner
# row this eventually drains into may not exist yet -- so there is no row PK to
# key it by. The drain runs from routers/container.py's status poll, through
# core/services/onboarding_brief.reconcile_pending_brief.
_PENDING_BRIEF_PREFIX = "__pending_brief__:"


def _pending_brief_key(owner_id: str) -> str:
    return f"{_PENDING_BRIEF_PREFIX}{owner_id}"


# The single "genuinely not started" onboarding-state shape -- SOLE owner
# of this key set. ``get_onboarding_state`` overlays the one dynamic field
# (`pending_brief`, computed per-call) on top of this; every other caller
# needing an all-defaults onboarding dict (notably
# ``core.services.readiness_state``'s degrade-to-defaults except branch)
# must import THIS rather than hand-copy the keys -- a hand-copy already
# drifted out of sync once (commit 6b42f427e / #1820) and silently dropped
# first_contact_*/friday_agent_id from the degraded-read shape.
ONBOARDING_STATE_DEFAULTS: dict = {
    "onboarding_generation": None,
    "funnel_state": "not_started",
    "pending_brief": False,
    "funnel_submitted_at": None,
    "funnel_completed_at": None,
    "onboarding_goal_id": None,
    "onboarding_project_id": None,
    "onboarding_brief_text": None,
    "onboarding_submitted_by": None,
    "first_contact_state": "not_started",
    "first_contact_attempts": 0,
    "first_contact_reason": None,
    "friday_agent_id": None,
    "teams_state": None,
    "company_id": None,
    "paperclip_user_id": None,
    "user_id": None,
}


@dataclass
class PaperclipCompany:
    """Per-USER row. ``company_id`` is shared across all rows belonging
    to the same Isol8 org; the other fields are per-user.
    """

    user_id: str
    """Isol8 user ID (Clerk user)."""

    org_id: str
    """Isol8 org ID (Clerk org). Identical for all rows in the same org."""

    company_id: str
    """Paperclip company ID. Same value for every Isol8 user in the
    same org — the Paperclip company is org-scoped, not user-scoped."""

    paperclip_user_id: str
    """Better Auth user ID inside Paperclip. Created via
    ``POST /api/auth/sign-up/email`` at provisioning time."""

    paperclip_password_encrypted: str
    """Fernet-encrypted random password for the Better Auth account.
    Used by the proxy router to sign the user in (server-to-server)
    and forward the Set-Cookie back to the browser."""

    service_token_encrypted: str
    """Fernet-encrypted long-lived OpenClaw service-token JWT. Baked
    into the seeded Paperclip agent's openclaw-gateway adapter so the
    agent can reach the user's own OpenClaw container via the
    existing WS gateway."""

    status: PaperclipCompanyStatus
    created_at: datetime
    updated_at: datetime

    paperclip_email: str = ""
    """The address Better Auth actually holds for ``paperclip_user_id``.

    Better Auth's ``user.email`` is written once at sign-up and NEVER
    updated, so it is NOT interchangeable with the user's live Clerk primary
    email: the moment a human moves mailbox domains, signing in with the
    Clerk address 401s, falls through to sign-UP, and mints a DUPLICATE
    Better Auth account (whose sign-in rate limit is per-IP, i.e. fleet-wide
    degradation). Every sign-in door reads this first and falls back to the
    live Clerk address only when it is empty (a row written before this
    field existed and not yet backfilled).

    Defaults to "" so pre-backfill rows read back unchanged.
    """

    last_error: Optional[str] = None
    scheduled_purge_at: Optional[datetime] = None
    seeded_agent_ids: Optional[list[str]] = None
    """Legacy Paperclip agent IDs from the Friday/Atlas founder seed.

    This marker is retained only for those pre-starter-roster companies. It
    marks agents protected by that historical provisioning contract and is used by
    ``paperclip_openclaw_pairing.is_seeded_agent`` and the Teams BFF
    ``seeded`` flag. Only the provisioner's row carries this (it's the
    row whose ``provision_org`` actually seeded the agents); members'
    rows leave it None. Matching on persisted IDs (rather than role/name)
    means a user-created agent that happens to use role ``ceo`` or the
    name ``Atlas`` is never mistaken for a legacy-seeded one, and renaming a
    seeded agent never drops its protection."""

    starter_agent_ids: Optional[dict[str, str]] = None
    """Starter role key -> Paperclip agent UUID for fresh workspaces.

    The keys are ``ceo``, ``engineering``, ``finance``, ``growth``,
    ``analytics``, and ``customer_operations``. Only the authoritative owner
    row carries this map. Unlike
    ``seeded_agent_ids``, it records provenance and reconciliation identity;
    it does not make an agent undeletable.
    """

    # ------------------------------------------------------------------
    # Onboarding (co-located per the 2026-07-24 architecture decision —
    # NO dedicated table; these fields ride on the SAME owner row as the
    # fields above). Only the OWNER row (the row ``provision_org`` writes
    # for the org creator / personal owner — see ``resolve_owner_row``)
    # carries meaningful values here; member rows (``provision_member``)
    # never set them and are never read for them.
    # ------------------------------------------------------------------

    onboarding_generation: Optional[str] = None
    """ULID fence for onboarding facts (the company-incarnation fence, G2).
    Minted exactly once, in ``provision_org``'s fresh-create branch, AFTER a
    genuinely-new Paperclip company is confirmed — never on reconcile-adopt
    of an existing ``company_id``, never on ``provision_member`` (INV-3). A
    write against a superseded generation is a silent no-op (INV-4)."""

    funnel_state: Optional[FunnelState] = None
    """``not_started`` -> ``submitted`` -> ``complete`` for first-message
    onboarding. The first human message in the canonical room claims the
    state, and fulfilment completes it only after the initial goal/project
    and ``BRIEF.md`` are staged."""

    funnel_submitted_at: Optional[datetime] = None
    funnel_completed_at: Optional[datetime] = None
    onboarding_goal_id: Optional[str] = None
    onboarding_project_id: Optional[str] = None

    onboarding_brief_text: Optional[str] = None
    """The captured first human message, written with the same CAS claim as
    its onboarding state. Fulfilment turns it into ``BRIEF.md`` for the
    normally dispatched agent. It is additive/backward-compatible: absent legacy values read
    as ``None`` and are cleared with the other claim-scoped fields on retry."""

    onboarding_submitted_by: Optional[str] = None
    """The Clerk member who sent the captured first message, written on the
    same claim as ``funnel_submitted_at``. It may differ from ``user_id`` for
    an org, whose owner row is keyed by the creator. For a personal owner they
    are the same. Cleared by ``revert_funnel_submission`` with the other
    claim-scoped fields."""

    first_contact_state: Optional[str] = None
    """``not_started`` -> ``sending`` -> ``sent`` | ``failed``.

    THE once-only guard for the agent-initiated first email. Policy is read
    from HERE, never from the ``purpose`` label a container supplies, so an
    agent cannot skip the guard by mislabelling its send — and does not have
    to make a judgment call it might get wrong.

    ``failed`` is deliberately RE-CLAIMABLE: a failed mint or a failed SES
    send must not make the first failure terminal. ``sending`` and ``sent``
    are not — reclaiming either risks a double-send."""

    first_contact_attempts: Optional[int] = None
    """Bounds the nudge loop, and is the ONLY thing that does. Nudge runs
    re-register the run-completion callback (otherwise attempts 2 and 3 would
    have no trigger — the bootstrap run terminates exactly once), so a nudge
    dispatched without incrementing this first is an infinite chain of agent
    runs."""

    first_contact_reason: Optional[str] = None


class PaperclipRepo:
    """Async repository for the paperclip-companies DynamoDB table.

    The table name is injected so the same repo class is usable for the
    real per-environment table (e.g. ``isol8-dev-paperclip-companies``)
    and for moto-backed unit tests. Internally we resolve the boto3
    Table via ``core.dynamodb.get_table`` so prefix-patching in tests
    Just Works.
    """

    def __init__(self, table_name: str, region: str | None = None):  # noqa: ARG002 - region kept for back-compat
        # ``region`` is accepted for back-compat with existing call sites
        # but ignored: the shared boto3 resource in core.dynamodb already
        # handles region + endpoint configuration.
        self._table_name = table_name

    def _table(self):
        return get_table(self._table_name)

    async def put(
        self,
        company: PaperclipCompany,
        *,
        condition: str | None = None,
        condition_values: dict | None = None,
    ) -> None:
        """Whole-item put — replaces the row entirely.

        ⚠️ WRITER-SAFETY (see the architecture spec's "load-bearing safety
        requirement", §2): this is a raw ``PutItem`` — any onboarding field
        NOT set on ``company`` is simply ABSENT from the new item, which
        silently WIPES a previously-written value (``onboarding_generation``,
        ``funnel_state``, onboarding ids, ...). Every caller of ``put``
        MUST carry forward the row's existing onboarding fields (read-merge)
        unless it is deliberately establishing them fresh (the G2 mint site
        in ``paperclip_provisioning.provision_org``). Prefer the
        generation-conditioned ``UpdateItem`` methods below
        (``claim_funnel_submission`` / ``record_onboarding_ids`` /
        ``revert_funnel_submission``) for any onboarding-only write — they
        cannot clobber unrelated fields by construction.

        ``condition``/``condition_values`` are an escape hatch used ONLY by the G2 mint site
        (read-then-put is not atomic): the caller passes
        ``attribute_not_exists(onboarding_generation) OR onboarding_generation = :g`` so a
        concurrent peer's already-persisted generation wins instead of being silently
        overwritten. On mismatch, boto3 raises ``ClientError``/``ConditionalCheckFailedException``
        (uncaught here — the caller re-reads and retries).
        """
        item = {
            "user_id": company.user_id,
            "org_id": company.org_id,
            "paperclip_user_id": company.paperclip_user_id,
            "paperclip_password_encrypted": company.paperclip_password_encrypted,
            "paperclip_email": company.paperclip_email,
            "service_token_encrypted": company.service_token_encrypted,
            "status": company.status,
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
        }
        # A not-yet-known company_id ("") must be OMITTED, never written as an
        # empty string: company_id keys the by-company-id GSI, and DynamoDB
        # rejects the whole PutItem when an index key attribute is "". The
        # sparse index skips such rows, which is also semantically right — a
        # pre-company row has no business in a companyId lookup.
        if company.company_id:
            item["company_id"] = company.company_id
        if company.last_error is not None:
            item["last_error"] = company.last_error
        if company.scheduled_purge_at is not None:
            item["scheduled_purge_at"] = company.scheduled_purge_at.isoformat()
        if company.seeded_agent_ids:
            item["seeded_agent_ids"] = company.seeded_agent_ids
        if company.starter_agent_ids:
            item["starter_agent_ids"] = company.starter_agent_ids
        if company.onboarding_generation is not None:
            item["onboarding_generation"] = company.onboarding_generation
        if company.funnel_state is not None:
            item["funnel_state"] = company.funnel_state
        if company.funnel_submitted_at is not None:
            item["funnel_submitted_at"] = company.funnel_submitted_at.isoformat()
        if company.funnel_completed_at is not None:
            item["funnel_completed_at"] = company.funnel_completed_at.isoformat()
        if company.onboarding_goal_id is not None:
            item["onboarding_goal_id"] = company.onboarding_goal_id
        if company.onboarding_project_id is not None:
            item["onboarding_project_id"] = company.onboarding_project_id
        if company.onboarding_brief_text is not None:
            item["onboarding_brief_text"] = company.onboarding_brief_text
        if company.onboarding_submitted_by is not None:
            item["onboarding_submitted_by"] = company.onboarding_submitted_by
        if company.first_contact_state is not None:
            item["first_contact_state"] = company.first_contact_state
        if company.first_contact_attempts is not None:
            item["first_contact_attempts"] = company.first_contact_attempts
        if company.first_contact_reason is not None:
            item["first_contact_reason"] = company.first_contact_reason
        put_kwargs: dict = {"Item": item}
        if condition:
            put_kwargs["ConditionExpression"] = condition
            if condition_values:
                put_kwargs["ExpressionAttributeValues"] = condition_values
        await run_in_thread(self._table().put_item, **put_kwargs)
        publish_status_changed(_broadcast_owner_key(user_id=company.user_id, org_id=company.org_id))

    async def get(self, user_id: str, *, consistent_read: bool = False) -> Optional[PaperclipCompany]:
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": user_id},
            ConsistentRead=consistent_read,
        )
        if "Item" not in resp:
            return None
        return self._item_to_company(resp["Item"])

    @staticmethod
    def _item_to_company(item: dict) -> PaperclipCompany:
        """Raw DDB item -> ``PaperclipCompany``. Factored out of ``get`` so
        ``get_onboarding_state``'s org-owner fallback (which resolves the
        provisioner's row via the ``by-org-id`` GSI's ``ProjectionType=ALL``
        query results, not a base-table ``get_item``) can build the same
        dataclass from a GSI-projected item without a second base-table
        read.

        The credential trio defaults to "" rather than strict-indexing: a
        durable ``failed`` marker (put_failed_marker_if_absent) deliberately
        carries NO credentials — its only job is to make a net-new seed
        failure visible (so /teams returns an honest 503) — and this must
        still read it back as a status="failed" row rather than KeyError.
        Real provisioned rows always carry all three (put() writes them).
        """
        return PaperclipCompany(
            user_id=item["user_id"],
            org_id=item["org_id"],
            # Absent on pre-company rows (seeding claims, failed markers) —
            # writers omit it rather than storing "" (illegal as a
            # by-company-id GSI key). Hydrate back to "" so consumers keep
            # their existing truthiness checks.
            company_id=item.get("company_id", ""),
            paperclip_user_id=item.get("paperclip_user_id", ""),
            paperclip_password_encrypted=item.get("paperclip_password_encrypted", ""),
            paperclip_email=item.get("paperclip_email", ""),
            service_token_encrypted=item.get("service_token_encrypted", ""),
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            last_error=item.get("last_error"),
            scheduled_purge_at=(
                datetime.fromisoformat(item["scheduled_purge_at"]) if item.get("scheduled_purge_at") else None
            ),
            seeded_agent_ids=(list(item["seeded_agent_ids"]) if item.get("seeded_agent_ids") else None),
            starter_agent_ids=(dict(item["starter_agent_ids"]) if item.get("starter_agent_ids") else None),
            onboarding_generation=item.get("onboarding_generation"),
            funnel_state=item.get("funnel_state"),
            funnel_submitted_at=(
                datetime.fromisoformat(item["funnel_submitted_at"]) if item.get("funnel_submitted_at") else None
            ),
            funnel_completed_at=(
                datetime.fromisoformat(item["funnel_completed_at"]) if item.get("funnel_completed_at") else None
            ),
            onboarding_goal_id=item.get("onboarding_goal_id"),
            onboarding_project_id=item.get("onboarding_project_id"),
            onboarding_brief_text=item.get("onboarding_brief_text"),
            onboarding_submitted_by=item.get("onboarding_submitted_by"),
            first_contact_state=item.get("first_contact_state"),
            first_contact_attempts=(
                int(item["first_contact_attempts"]) if item.get("first_contact_attempts") is not None else None
            ),
            first_contact_reason=item.get("first_contact_reason"),
        )

    async def get_onboarding_state(self, owner_id: str) -> dict:
        """RECONCILE-WITH-ONBOARD: read-only accessor for the
        COMPOSER/ONBOARD interface contract.

        Returns the onboarding keys plus ``teams_state`` (the row's raw
        ``status``), ``company_id``, ``paperclip_user_id``, and ``user_id``
        (this row's PK — the org creator for an org owner, not necessarily
        ``owner_id``). This lets ``core.services.readiness_state.compute_status``
        and the pending-brief reconciler share one physical Paperclip read
        where possible (one read, not two).

        ``owner_id`` is the CONTAINER owner (org_id for an org, else the
        user_id) -- NOT necessarily a row key. This table's PK is a Clerk
        user_id (see module docstring): for a personal owner
        (user_id == owner_id) ``self.get(owner_id)`` already resolves the
        right row directly (the common case, ONE physical read). For an
        ORG owner (``owner_id`` is an ``org_`` id) there is no row keyed by
        org_id itself, so this falls back to the ``by-org-id`` GSI (also
        ONE physical read -- ``ProjectionType=ALL``, no base-table chain
        read needed) and returns the PROVISIONER's row -- the one row
        ``starter_agent_ids`` was stamped on at provision time — since
        onboarding facts are company-wide, not per-member. Falls back to the
        first row if none carries a starter or legacy seeded marker (defensive;
        shouldn't happen for an
        actively-provisioned org).

        Owner-row resolution is delegated to ``resolve_owner_row`` -- the
        single canonical "which row is THE owner row" path shared with
        ``routers/container.py`` and the provisioning executor -- so
        org vs. personal resolution has exactly one implementation.

        A row that doesn't exist yet, or that predates ONBOARD's writers,
        returns every onboarding field at its "genuinely not started"
        default -- never raises (read failures are the caller's
        responsibility to guard, matching every other method here).
        """
        # include_inactive=True: a status reader must still resolve a
        # FAILED org seed row so teams_state="failed" surfaces (the sign-in
        # resolver's active-only filter would hide it). Personal owners already
        # resolve via a status-agnostic get.
        company = await self.resolve_owner_row(owner_id, include_inactive=True)
        if company is None:
            # ONBOARDING_STATE_DEFAULTS covers every static field; only
            # `pending_brief` is dynamic (per-owner DDB lookup) and gets
            # overlaid here.
            return {
                **ONBOARDING_STATE_DEFAULTS,
                "pending_brief": await self._has_pending_brief(owner_id),
            }

        # getattr(..., default) rather than direct attribute access: real
        # PaperclipCompany instances always have every field (defaulted by
        # the dataclass), but a handful of existing test doubles construct
        # a bare SimpleNamespace(status=...) standing in for `repo.get`'s
        # return value -- getattr keeps this method correct against both
        # without forcing every such test double to be rewritten into a
        # full PaperclipCompany just to add a status field.
        # The onboarding dataclass carries funnel timestamps as
        # ``datetime`` and defaults the state literals to ``None`` (not the
        # "not_started" string). This accessor is the COMPOSER/ONBOARD bridge:
        # it normalizes those to the contract's string-typed shape -- ISO
        # strings for timestamps, "not_started" for an unset state -- so
        # ``compute_status`` and the StatusContract never see a bare datetime
        # or a null state.
        def _iso(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        funnel_state = getattr(company, "funnel_state", None) or "not_started"

        return {
            "onboarding_generation": getattr(company, "onboarding_generation", None),
            "funnel_state": funnel_state,
            # Gated on "not yet COMPLETE", not on "still not_started". The
            # tighter gate looked cheaper and was wrong: a claim that landed
            # and then died mid-seed (a rolling deploy, a cancelled task)
            # leaves the row at "submitted" forever, and gating on
            # "not_started" made the still-undrained brief invisible to the
            # drain from that moment on -- permanently, with no recovery
            # path. "complete" is the one state that proves fulfilment
            # finished (the initial goal/project and BRIEF.md are staged), so
            # it is the only safe place to stop looking.
            #
            # Cost: one small GetItem per poll for owners below "complete" --
            # the pre-onboarding window, plus legacy rows that onboarded via
            # the old containers.onboarded_at stamp and so sit below it
            # forever. The steady-state fleet skips it entirely.
            "pending_brief": (await self._has_pending_brief(owner_id) if funnel_state != "complete" else False),
            "funnel_submitted_at": _iso(getattr(company, "funnel_submitted_at", None)),
            "funnel_completed_at": _iso(getattr(company, "funnel_completed_at", None)),
            "onboarding_goal_id": getattr(company, "onboarding_goal_id", None),
            "onboarding_project_id": getattr(company, "onboarding_project_id", None),
            "onboarding_brief_text": getattr(company, "onboarding_brief_text", None),
            "onboarding_submitted_by": getattr(company, "onboarding_submitted_by", None),
            "first_contact_state": getattr(company, "first_contact_state", None) or "not_started",
            "first_contact_attempts": getattr(company, "first_contact_attempts", None) or 0,
            "first_contact_reason": getattr(company, "first_contact_reason", None),
            # Fresh workspaces identify the CEO explicitly. Legacy founder
            # workspaces retain their stable first seeded id as a fallback for
            # first-contact mail, so both paths stay on this same read.
            "friday_agent_id": (getattr(company, "starter_agent_ids", None) or {}).get("ceo")
            or (getattr(company, "seeded_agent_ids", None) or [None])[0],
            "teams_state": getattr(company, "status", None),
            "company_id": getattr(company, "company_id", None),
            "paperclip_user_id": getattr(company, "paperclip_user_id", None),
            "user_id": getattr(company, "user_id", None),
        }

    async def get_org_company_id(self, org_id: str) -> Optional[str]:
        """Return the shared ``company_id`` for any one user in this org.

        Used at member-join time (``organizationMembership.created``):
        if any other user in the org already has a row, we reuse their
        company_id rather than creating a new Paperclip company.

        Implementation: queries the ``by-org-id`` GSI (PK: ``org_id``) and
        returns the first row that CARRIES a ``company_id``. All real rows in
        the same org are required (by the provisioner) to carry the same
        ``company_id``, so any one of them is correct — but a bare seed-claim
        row (``try_acquire_seed_claim``, owner or member) sits in this index
        with no ``company_id`` at all, and must never shadow the real rows:
        a member joining an org that already has a company would otherwise
        be told it has none while a peer's claim is live (or stale).

        Why a GSI rather than a scan: while v1 traffic is small enough
        a scan would work, member-join can fire repeatedly during
        bulk org imports and a GSI lookup is O(matches) instead of
        O(table). The GSI is declared in
        ``database-stack.ts`` alongside the existing ``by-status-purge-at``
        index, with ``ProjectionType: KEYS_ONLY`` (so reads only need
        ``user_id`` to chain into a base-table ``get`` if more fields
        are needed) — but for ``get_org_company_id`` we project
        ``company_id`` via ``ALL`` so a single GSI query suffices.
        """
        async for page in self._query_pages(
            IndexName="by-org-id",
            KeyConditionExpression=Key("org_id").eq(org_id),
        ):
            for item in page.get("Items", []):
                if item.get("company_id"):
                    return item["company_id"]
        return None

    async def get_user_ids_for_company(self, company_id: str) -> list[str]:
        """Return the Clerk user_ids of every ACTIVE row in ``company_id``.

        Push-notification audience for inbound Paperclip plugin events
        (``routers/paperclip_events.py``): a personal company has exactly one
        row; an org company has one row per member, all sharing the same
        ``company_id``. Queries the ``by-company-id`` GSI (PK: ``company_id``,
        projection INCLUDE ``status``) declared in ``database-stack.ts``.
        """
        user_ids: list[str] = []
        async for page in self._query_pages(
            IndexName="by-company-id",
            KeyConditionExpression=Key("company_id").eq(company_id),
        ):
            user_ids.extend(
                item["user_id"]
                for item in page.get("Items", [])
                if item.get("status") == "active" and item.get("user_id")
            )
        return user_ids

    async def _query_pages(self, **kwargs):
        """Yield each raw Query response page, following ``LastEvaluatedKey``.

        The one pagination loop for every GSI reader in this repo (Query caps
        a page at ~1MB regardless of result size). For typical Isol8 org
        sizes (1-10 users) one page suffices.
        """
        while True:
            resp = await run_in_thread(self._table().query, **kwargs)
            yield resp
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs = {**kwargs, "ExclusiveStartKey": last_key}

    async def list_by_org_id(self, org_id: str) -> list[dict]:
        """Return all rows in ``org_id`` via the ``by-org-id`` GSI.

        Returns raw DDB items (dicts) rather than ``PaperclipCompany``
        instances because callers (notably the Teams BFF members
        endpoint) typically only need a small subset of fields
        (``user_id``, ``paperclip_user_id``) to build a Paperclip→Clerk
        principal map. The GSI is declared with ``ProjectionType=ALL``
        in ``database-stack.ts`` so every field is available without a
        base-table chain read.

        """
        items: list[dict] = []
        async for page in self._query_pages(
            IndexName="by-org-id",
            KeyConditionExpression=Key("org_id").eq(org_id),
        ):
            items.extend(page.get("Items", []))
        return items

    async def count_org_members(self, org_id: str) -> int:
        """Count how many rows currently exist for ``org_id`` via the
        ``by-org-id`` GSI.

        Used by ``PaperclipProvisioning.purge`` to decide whether to
        archive the underlying Paperclip company: callers delete the
        target user's row FIRST, then call this — a result of 0 means
        the purged user was the last member and the company can be
        archived; anything else means co-tenants still need it.

        Implementation: GSI Query with ``Select="COUNT"`` so DynamoDB
        returns just the count without materializing items; paginated
        because Query's per-page cap would otherwise undercount.
        """
        count = 0
        async for page in self._query_pages(
            IndexName="by-org-id",
            KeyConditionExpression=Key("org_id").eq(org_id),
            Select="COUNT",
        ):
            count += int(page.get("Count", 0))
        return count

    async def update_status(
        self,
        user_id: str,
        *,
        status: PaperclipCompanyStatus,
        last_error: Optional[str] = None,
        scheduled_purge_at: Optional[datetime] = None,
        not_if_status: Optional[PaperclipCompanyStatus] = None,
        require_seeding_claim_id: Optional[str] = None,
        owner_id: Optional[str] = None,
        company_id: Optional[str] = None,
    ) -> bool:
        """Update a row's status. Returns True iff the write landed.

        ``company_id`` (when truthy) is stamped alongside the status. The one
        caller is ``_mark_failed`` downgrading a bare seed-claim row: the
        claim carries no company_id (none exists for an owner at claim time),
        but a MEMBER's claim fails inside a known company, and the failed
        marker must keep carrying it — same shape ``put_failed_marker_if_absent``
        writes — because ``archive_member`` lists the company's members off
        that field when the seat is later removed. Never written as "" (an
        empty by-company-id GSI key is rejected outright).

        ``not_if_status`` guards the write with a ConditionExpression so the
        update is SKIPPED (returns False) when the row is currently in that
        status. Used so a losing concurrent provision attempt can't downgrade
        an already-``active`` row to ``failed``: two overlapping ``provision_member``
        attempts for one member can race (e.g. /teams + /chat both reconciling),
        and the loser's failure path must not clobber the winner's active row.

        ``require_seeding_claim_id`` additionally fences the write on the bare
        seed-claim row's ``seeding_claim_id`` — the write is SKIPPED unless the
        row EITHER carries no ``seeding_claim_id`` (it advanced past the bare
        claim into a real ``provisioning``/``failed``/etc. row) OR its
        ``seeding_claim_id`` matches. Without it, a stale seed holder's failure
        path could flip a NEWER holder's live ``seeding`` row (a stolen claim)
        to ``failed`` — the token ``try_acquire_seed_claim`` mints exists
        precisely so the downgrade can be fenced on it.

        ``owner_id`` is the realtime-nudge broadcast key (org_id for orgs, else
        the personal user_id) -- the value frontend tabs register under. It is
        NOT the row PK (``user_id``), which for an org is the creator's personal
        id where no org tab is registered. Callers holding org context pass the
        row's ``org_id``; when omitted the nudge falls back to ``user_id``
        (always correct for personal owners).
        """
        update_expr = "SET #s = :s, updated_at = :u"
        expr_names = {"#s": "status"}
        expr_values: dict = {
            ":s": status,
            ":u": utc_now_iso(),
        }
        if last_error is not None:
            update_expr += ", last_error = :e"
            expr_values[":e"] = last_error
        if scheduled_purge_at is not None:
            update_expr += ", scheduled_purge_at = :p"
            expr_values[":p"] = scheduled_purge_at.isoformat()
        if company_id:
            update_expr += ", company_id = :c"
            expr_values[":c"] = company_id

        kwargs: dict = {
            "Key": {"user_id": user_id},
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": expr_names,
            "ExpressionAttributeValues": expr_values,
        }
        conditions: list[str] = []
        if not_if_status is not None:
            # #s already maps to "status"; reuse it for the guard. Also require
            # the row to still exist (a deleted row shouldn't be re-created here).
            conditions.append("attribute_exists(user_id) AND #s <> :guard")
            expr_values[":guard"] = not_if_status
        if require_seeding_claim_id is not None:
            # A row that advanced past the bare claim dropped seeding_claim_id;
            # a bare claim keeps it. Fence only the bare-claim case on a match so
            # a stale holder can't downgrade a newer holder's stolen claim.
            conditions.append("(attribute_not_exists(seeding_claim_id) OR seeding_claim_id = :cid)")
            expr_values[":cid"] = require_seeding_claim_id
        if conditions:
            kwargs["ConditionExpression"] = " AND ".join(conditions)

        table = self._table()
        try:
            await run_in_thread(table.update_item, **kwargs)
            publish_status_changed(owner_id or user_id)
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def put_failed_marker_if_absent(
        self,
        *,
        user_id: str,
        org_id: str,
        reason: str,
        company_id: str = "",
    ) -> bool:
        """Durably record "a seed failed here" when NO row exists yet, via a
        CONDITIONAL put-if-absent (``attribute_not_exists(user_id)``).

        Returns True iff the marker landed (the key was free); False when a real
        row already exists — a concurrent provision wrote it in the gap after the
        caller's ``get()`` returned None, and the conditional REFUSES to clobber
        it. That refusal is the whole point: the old unconditional empty-id put
        overwrote a just-written provisioning row's real ``paperclip_user_id``
        with "" on an eventually-consistent stale read, wedging adoption. A
        put-if-absent can only ever create, never overwrite.

        Deliberately carries NO credential fields — ``paperclip_user_id`` /
        ``paperclip_password_encrypted`` / ``service_token_encrypted`` are ABSENT,
        not "". The marker's only job is durable visibility of the failure so
        ``/teams`` returns an honest 503 (and ``/container/status`` surfaces
        ``teams_status="failed"``) instead of the 202 spinner a MISSING row
        produces. It is NOT adoptable: the net-new / orphan-422 failures that
        reach the no-row branch (Better Auth user may already exist with an
        unknown id) cannot self-heal a retry, and a truthful failed row is the
        honest terminal outcome. ``get()`` tolerates the absent credentials.
        """
        now = utc_now_iso()
        item = {
            "user_id": user_id,
            "org_id": org_id,
            "status": "failed",
            "last_error": reason,
            "created_at": now,
            "updated_at": now,
        }
        # Omitted (not "") when unknown — "" is illegal as a by-company-id
        # GSI key and would reject the whole put. See put().
        if company_id:
            item["company_id"] = company_id
        table = self._table()
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def delete(self, user_id: str) -> None:
        await run_in_thread(
            self._table().delete_item,
            Key={"user_id": user_id},
        )

    async def try_acquire_org_create_claim(
        self,
        org_id: str,
        *,
        holder: str,
        stale_after_seconds: int = 600,
    ) -> Optional[str]:
        """Strongly-consistent "I am creating this org's company" claim.

        Returns a fresh per-ATTEMPT fencing ``token`` iff THIS caller now holds
        the claim — by winning a fresh claim, or by stealing one whose holder
        went silent for longer than ``stale_after_seconds``. Returns None when
        another caller holds a non-stale claim.

        The token — NOT ``holder`` — is what ``release_org_create_claim`` fences
        on. ``holder`` is the caller's ``user_id``, and the SAME admin re-polls
        ``/container/status`` with the SAME user_id, so a holder-only release
        fence is subject to ABA: after a hard-crashed holder's claim is stolen
        by that same admin's later poll (a fresh attempt), a resurrected
        straggler from the FIRST attempt would still match ``holder`` and delete
        the live claim — reopening the create window that mints a duplicate
        org / split workspace (the incident this claim exists to prevent).
        A per-attempt token makes the straggler's release a clean no-op.

        Steal threshold rationale (why this is liveness evidence, not naive
        wall-time): the only operation a holder performs while holding the
        claim is ``provision_org``, a fixed sequence of ~9 awaited Paperclip
        HTTP calls. Each call is bounded by the provisioning client's 15s
        timeout (``build_paperclip_provisioning``), and any call that hits its
        timeout RAISES — which propagates to ``ensure_user_ready`` and
        RELEASES the claim. So a live create cannot stay held beyond roughly
        ~135s (9 × 15s) without releasing. The default 600s is comfortably
        past that ceiling, so a claim older than it cannot belong to a
        still-running create — it can only be a holder that crashed hard
        (SIGKILL, e.g. an ECS task replaced mid-deploy) before its except/
        release ran. Stealing then is safe. (If the client timeout is ever
        raised substantially, raise this in step.)

        This is a PK conditional write, so it is strongly consistent and
        cross-instance-safe — it closes the double-create window that the
        eventually-consistent ``by-org-id`` GSI (used by ``find_org_owner``)
        leaves open. Callers MUST ``release_org_create_claim`` on create
        FAILURE so a retry can proceed; on success the claim is left in place
        as a permanent "org bootstrapped" marker (by then ``find_org_owner``
        returns the real company and this path is not re-entered).
        """
        key = _org_create_claim_key(org_id)
        now = datetime.now(timezone.utc)
        token = uuid4().hex
        # NOTE: the identity is stored under ``claimed_by``, NOT ``holder``.
        # During a rolling deploy an OLD worker still runs the pre-token
        # ``release_org_create_claim`` whose condition is ``holder = :h`` — and
        # since the SAME admin re-polls with the same user_id, that stale release
        # would delete THIS attempt's token-fenced claim (an ABA delete across
        # versions). Omitting the ``holder`` attribute makes that old condition
        # ``attribute_not_exists`` → false, so the cross-version stale release
        # can no longer delete a token-fenced claim. ``claimed_by`` is kept only
        # for observability; nothing conditions on it.
        item = {
            "user_id": key,
            "kind": "org_create_claim",
            "claimed_by": holder,
            "claim_token": token,
            "claimed_at": now.isoformat(),
        }
        table = self._table()

        # 1. Fresh claim — wins iff no sentinel exists yet.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
            return token
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        # 2. A sentinel exists — strongly-consistent read, then steal only if
        #    its holder has gone stale.
        resp = await run_in_thread(table.get_item, Key={"user_id": key}, ConsistentRead=True)
        existing = resp.get("Item")
        if existing is None:
            # Released (deleted) between our put and get — try one fresh claim.
            try:
                await run_in_thread(
                    table.put_item,
                    Item=item,
                    ConditionExpression="attribute_not_exists(user_id)",
                )
                return token
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                return None

        prev_claimed_at = existing.get("claimed_at", "")
        try:
            age = (now - datetime.fromisoformat(prev_claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")  # unparseable timestamp → treat as stale
        if age < stale_after_seconds:
            return None

        # Steal — overwrite only if claimed_at is unchanged since our read, so
        # two concurrent stealers can't both win. The put rewrites claim_token
        # to THIS attempt's fresh token, invalidating any prior holder's release.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="claimed_at = :prev",
                ExpressionAttributeValues={":prev": prev_claimed_at},
            )
            return token
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return None

    async def release_org_create_claim(self, org_id: str, *, token: str) -> None:
        """Delete the per-org create claim iff ``token`` still holds it.

        Called on create FAILURE so the next poll can re-acquire and retry.
        Fenced on the per-ATTEMPT ``claim_token`` (returned by
        ``try_acquire_org_create_claim``), NOT on ``holder``: the same admin
        re-polls with the same user_id, so a holder-only fence let one
        identity's stale straggler delete a newer attempt's live claim (ABA).
        Best-effort: a lost fence is a no-op.
        """
        key = _org_create_claim_key(org_id)
        table = self._table()
        try:
            await run_in_thread(
                table.delete_item,
                Key={"user_id": key},
                ConditionExpression="claim_token = :t",
                ExpressionAttributeValues={":t": token},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    async def try_acquire_seed_claim(
        self,
        user_id: str,
        *,
        org_id: str,
        claim_id: str,
        stale_after_seconds: int = 300,
        company_id: str = "",
    ) -> bool:
        """Strongly-consistent "I am seeding this owner's company" claim on
        the REAL row PK — the fresh-create fence for ``provision_org`` and,
        via ``provision_member``, for a member joining an existing company.

        Returns True iff THIS caller now holds the claim — by winning a fresh
        put-if-absent, or by stealing a bare ``status="seeding"`` claim whose
        holder went silent for longer than ``stale_after_seconds``. Returns
        False when the row exists in any other state (a peer's seed is in
        flight, already landed, or already failed — the caller decides what
        to do by re-reading).

        Why this exists: ``provision_org``'s idempotency used to be
        check-then-act on this row — two concurrent calls on DIFFERENT
        backend instances could both read "no row" and both run the full
        Paperclip seed (Better-Auth sign-up, company create, starter roster),
        minting duplicate same-email users + duplicate companies (observed
        live on dev 2026-07-24, twice, 102ms and 358ms apart). Like
        ``try_acquire_org_create_claim`` this is a PK conditional write, so
        it is strongly consistent and cross-instance-safe — but it fences
        the PERSONAL-owner path too (the org-create claim only covers orgs),
        and it lives on the real row so the loser can simply poll it.

        Claims retain staged encrypted signup credentials across retries and
        stale-lease takeover. No Paperclip user ID exists until signup or
        authenticated recovery returns. A member claim also carries the
        known company_id so membership removal can retire it mid-provision.

        Steal threshold rationale (mirrors ``try_acquire_org_create_claim``):
        a live ``provision_org`` chain is ~9 awaited Paperclip HTTP calls,
        each bounded by the provisioning client's 15s timeout, and any
        timeout RAISES — which downgrades the row via ``_mark_failed``. So a
        row still ``seeding`` past ~300s can only be a holder that crashed
        hard (SIGKILL) between the claim and its first durable write; the
        steal is fenced on ``seeding_claimed_at`` being unchanged since our
        read so two concurrent stealers can't both win.
        """
        now = datetime.now(timezone.utc)
        item = {
            "user_id": user_id,
            "org_id": org_id,
            "status": "seeding",
            "seeding_claim_id": claim_id,
            "seeding_claimed_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        # Owner claims omit company_id (none exists yet, and "" would be
        # rejected outright as an empty by-company-id GSI key; get() hydrates
        # the absent attribute back to ""). Member claims carry the known one.
        if company_id:
            item["company_id"] = company_id
        table = self._table()

        # 1. Fresh claim — wins iff no row exists yet.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(user_id)",
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        # 2. Reclaim a failed signup without a confirmed identity, or a stale
        #    seeding claim, retaining its staged credentials. Other rows are
        #    handled by the caller rather than overwritten here.
        resp = await run_in_thread(table.get_item, Key={"user_id": user_id}, ConsistentRead=True)
        existing = resp.get("Item")
        if existing is None:
            # Deleted between our put and get — try one fresh claim.
            try:
                await run_in_thread(
                    table.put_item,
                    Item=item,
                    ConditionExpression="attribute_not_exists(user_id)",
                )
                return True
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                return False

        # An interrupted signup may already have created the remote account.
        # Its exact password/email must survive reclaim; generating another
        # password would make that account permanently unadoptable.
        credential_conditions = []
        credential_values = {}
        for field in ("paperclip_password_encrypted", "paperclip_email"):
            if field in existing:
                credential_conditions.append(f"{field} = :{field}")
                credential_values[f":{field}"] = existing[field]
                if existing[field]:
                    item[field] = existing[field]
            else:
                credential_conditions.append(f"attribute_not_exists({field})")
        # Staging can land after this read without changing the lease timestamp.
        # Defer takeover if either credential changed instead of erasing it.
        credential_condition = " AND ".join(credential_conditions)

        status = existing.get("status")
        if status == "failed" and not existing.get("paperclip_user_id"):
            # No confirmed identity yet: signup failed or its response was
            # lost. Retain any staged password for authenticated recovery.
            # The next attempt re-runs the sign-up — and
            # two concurrent retries would each sign up, reopening the exact
            # duplicate-account race this claim exists to close. Reclaim it
            # atomically, fenced on it still being credential-less: a failed
            # row that DOES carry ``paperclip_user_id`` is adopted (signed in
            # with its stored password), never re-signed-up, so it needs no
            # fence and is left alone. "Credential-less" must match the
            # Python pre-check above (absent OR empty): older markers stored
            # ``paperclip_user_id = ""`` explicitly, and ``attribute_not_exists``
            # is FALSE for an attribute that exists as "" — without the OR, a
            # legacy marker would pass the pre-check, fail this condition,
            # and wedge every retry in the loser wait forever.
            try:
                await run_in_thread(
                    table.put_item,
                    Item=item,
                    ConditionExpression=(
                        "#s = :failed AND (attribute_not_exists(paperclip_user_id) OR paperclip_user_id = :empty) "
                        f"AND {credential_condition}"
                    ),
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":failed": "failed", ":empty": "", **credential_values},
                )
                return True
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                return False

        if status != "seeding":
            return False

        prev_claimed_at = existing.get("seeding_claimed_at", "")
        try:
            age = (now - datetime.fromisoformat(prev_claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")  # unparseable timestamp → treat as stale
        if age < stale_after_seconds:
            return False

        # Steal — fenced on BOTH the status (still a bare claim) and the
        # claimed_at we read, so a concurrent winner's progress or a rival
        # stealer's win makes this a clean loss.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression=f"#s = :seeding AND seeding_claimed_at = :prev AND {credential_condition}",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":seeding": "seeding", ":prev": prev_claimed_at, **credential_values},
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def stage_signup_credentials(self, user_id: str, *, email: str, password_encrypted: str) -> tuple[str, str]:
        """Persist the signup secret before sending it.

        NOT fenced on any claim — two concurrent provision runs must both
        succeed and converge. ``if_not_exists`` is the convergence primitive:
        whichever run's write lands first fixes the value, and
        ``ReturnValues=ALL_NEW`` hands every caller that same converged
        (email, password) pair, so a retry never mints a second random
        password for an account that may already have signed up with the
        first one."""
        response = await run_in_thread(
            self._table().update_item,
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET paperclip_email = if_not_exists(paperclip_email, :email), "
                "paperclip_password_encrypted = if_not_exists(paperclip_password_encrypted, :password)"
            ),
            ConditionExpression="#s = :seeding",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":seeding": "seeding",
                ":email": email,
                ":password": password_encrypted,
            },
            ReturnValues="ALL_NEW",
        )
        item = response["Attributes"]
        return item["paperclip_email"], item["paperclip_password_encrypted"]

    async def find_owner_row(self, org_id: str, *, include_inactive: bool = False) -> Optional[PaperclipCompany]:
        """Find the ORG owner row (oldest ACTIVE ``created_at`` for this org_id).

        Deliberately a separate, repo-level implementation of the same
        query ``PaperclipProvisioning.find_org_owner`` runs (rather than a
        refactor of that tested, load-bearing method) -- see
        ``resolve_owner_row`` for why the repo needs this independently of
        provisioning: onboarding reads/writes (``routers/container.py``'s
        status poll and ``core/services/onboarding_brief.py``) need to resolve
        an ``owner_id`` to the row that carries onboarding truth WITHOUT going
        through the provisioning orchestrator.

        Filters to ``status="active"`` before picking the oldest, mirroring
        ``find_org_owner``: a disabled/provisioning/failed row is not
        sign-in-able and must never be selected over a genuinely active one.

        ``include_inactive`` (default False) relaxes that for onboarding/teams
        STATUS readers (``get_onboarding_state``): an org whose Paperclip seed
        FAILED has only a ``status="failed"`` owner row, and the caller needs
        it to surface ``teams_state="failed"`` (the Teams-degraded affordance).
        When no active row exists and ``include_inactive`` is set, the
        provisioner's row (the one carrying ``seeded_agent_ids``, else the
        oldest) is returned regardless of status. The sign-in path leaves this
        False so a non-active row is never selected over an active one.

        Returns None if the org has no rows (or no active row when
        ``include_inactive`` is False).
        """
        items: list[dict] = []
        last_evaluated_key: Optional[dict] = None
        table = self._table()
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(table.query, **kwargs)
            items.extend(resp.get("Items", []))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                break

        def _sort_key(it: dict) -> str:
            return it.get("created_at", "")

        active_items = [it for it in items if it.get("status") == "active"]
        if active_items:
            marked = [it for it in active_items if it.get("starter_agent_ids") or it.get("seeded_agent_ids")]
            chosen = min(marked or active_items, key=_sort_key)
        elif include_inactive and items:
            # No active row -- return the provisioner's row (carries
            # seeded_agent_ids) if present, else the oldest, so a failed-seed
            # org still resolves to a row carrying its onboarding/teams truth.
            marked = [it for it in items if it.get("starter_agent_ids") or it.get("seeded_agent_ids")]
            chosen = min(marked or items, key=_sort_key)
        else:
            return None
        # Hydrate straight from the GSI query item -- the by-org-id index is
        # ProjectionType.ALL, so `chosen` already carries every attribute
        # _item_to_company needs (that helper was factored out of get() for
        # exactly this). Avoids a second base-table read on the hot
        # /container/status org poll (get_onboarding_state calls this on every
        # ~3s poll). Same eventual-consistency tier the base get() had on the
        # org path (the GSI can't be a consistent read anyway -- see
        # resolve_owner_row); the brief-flow CAS write fences on generation, so
        # a briefly-stale read can't corrupt the claim.
        return self._item_to_company(chosen)

    async def resolve_owner_row(
        self, owner_id: str, *, consistent_read: bool = False, include_inactive: bool = False
    ) -> Optional[PaperclipCompany]:
        """Resolve an ``owner_id`` (Clerk org_id for orgs, Clerk user_id for
        personal owners) to the paperclip-companies OWNER row that carries
        onboarding truth.

        The table is keyed by ``user_id``, not ``owner_id``. For a personal
        owner the two are the same value, so the row IS keyed by
        ``owner_id`` directly. For an org, the row ``provision_org`` wrote
        is keyed by the org CREATOR's ``user_id`` (``owner_user_id`` in that
        method) -- a different value from ``owner_id`` (the Clerk org_id) --
        so it must be found via ``find_owner_row``'s by-org-id GSI lookup.

        This is the single resolution path every onboarding reader/writer
        (first-message capture and ``routers/container.py``'s ``onboarded``
        computation) should use, so "which row is THE owner row" has one
        answer.

        ``consistent_read`` defaults to False (eventually-consistent). The
        HOT caller is ``get_onboarding_state`` on every ``/container/status``
        poll (~3s/owner), where ``onboarded`` is best-effort and a briefly
        stale ``False`` is harmless -- a strong read there would double the
        ``paperclip-companies`` RCU cost fleet-wide for a field that flips
        once. The one caller that needs strong consistency is
        first-message fulfilment (the onboarding claim reads then writes the
        same row); it passes ``consistent_read=True``. NOTE: the org path
        goes through the ``by-org-id`` GSI, which is eventually consistent by
        construction -- ``consistent_read`` only strengthens the personal
        (base-table ``get``) path.
        """
        if is_org_owner_id(owner_id):
            return await self.find_owner_row(owner_id, include_inactive=include_inactive)
        return await self.get(owner_id, consistent_read=consistent_read)

    async def remove_starter_agent_id(self, owner_id: str, agent_id: str) -> None:
        """Remove the matching starter identity from the authoritative row.

        The value condition makes a stale delete harmless if reconciliation
        has already replaced that role with a new UUID.
        """
        company = await self.resolve_owner_row(owner_id, include_inactive=True)
        if company is None or not company.starter_agent_ids:
            return
        starter_key = next((key for key, value in company.starter_agent_ids.items() if value == agent_id), None)
        if starter_key is None:
            return
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": company.user_id},
                UpdateExpression="REMOVE starter_agent_ids.#starter_key",
                ConditionExpression="starter_agent_ids.#starter_key = :agent_id",
                ExpressionAttributeNames={"#starter_key": starter_key},
                ExpressionAttributeValues={":agent_id": agent_id},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    async def claim_first_contact(self, user_id: str, *, onboarding_generation: str) -> bool:
        """Conditional ``first_contact_state -> sending``, generation-fenced.

        THE once-only guard for the agent-initiated first email. Only the
        caller whose write lands sends; a loser reports ``already_sent``
        without touching SES, so an owner can never receive two.

        Claimable from ``not_started`` (never sent) and from ``failed`` (a
        failed mint or SES send must be retryable, or the FIRST failure is
        terminal and the bounded nudge retry never fires). NOT claimable from
        ``sending`` (a claim is in flight) or ``sent`` (terminal) — either
        would risk a double-send.

        A stuck ``sending`` — the process died between claim and settle — is
        deliberately not recovered here. Recovery belongs to an observer that
        can date the claim, not to a second escape hatch inside the claim
        itself.

        Never raises.
        """
        now = utc_now_iso()
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression=("SET first_contact_state = :sending, first_contact_claimed_at = :t, updated_at = :t"),
                ConditionExpression=(
                    "onboarding_generation = :gen AND "
                    "(attribute_not_exists(first_contact_state) "
                    "OR first_contact_state = :not_started "
                    "OR first_contact_state = :failed)"
                ),
                ExpressionAttributeValues={
                    ":sending": "sending",
                    ":not_started": "not_started",
                    ":failed": "failed",
                    ":gen": onboarding_generation,
                    ":t": now,
                },
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Lost the race, wrong generation, or already sending/sent.
            return False
        except Exception as e:  # noqa: BLE001 — must not 500 the send path
            logger.warning("claim_first_contact failed for %s: %s", user_id, e)
            return False

    async def settle_first_contact(
        self,
        user_id: str,
        *,
        onboarding_generation: str,
        state: str,
        reason: Optional[str] = None,
    ) -> None:
        """Terminal write: ``sent`` or ``failed``.

        Generation-fenced so a straggler from a superseded onboarding attempt
        can never resolve a later one. Never raises — the send already
        happened (or didn't), and a bookkeeping failure must not turn a
        delivered email into a 500.
        """
        now = utc_now_iso()
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression=("SET first_contact_state = :s, first_contact_reason = :r, updated_at = :t"),
                ConditionExpression="onboarding_generation = :gen",
                ExpressionAttributeValues={
                    ":s": state,
                    ":r": reason or "",
                    ":gen": onboarding_generation,
                    ":t": now,
                },
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Superseded generation — a straggler must not resolve a later attempt.
            pass
        except Exception as e:  # noqa: BLE001 — bookkeeping must not undo a send
            logger.warning("settle_first_contact failed for %s: %s", user_id, e)

    async def claim_funnel_submission(
        self,
        user_id: str,
        *,
        onboarding_generation: str,
        submitted_by: str,
        owner_id: Optional[str] = None,
        brief_text: Optional[str] = None,
    ) -> FunnelClaimResult:
        """Conditional ``funnel_state: not_started -> submitted``, fenced on
        ``onboarding_generation`` (INV-4). Replaces the ``__onboarded__``
        sentinel's ``claim_onboarding`` -- see ``FunnelClaimResult`` for why
        this is now a single atomic UpdateItem on the row itself instead of
        a separate claim item with its own stale-steal timing: DynamoDB's
        conditional write already serializes concurrent submitters, and the
        two ways to lose (superseded generation vs. already submitted) are
        distinguished by one strongly-consistent re-read on failure.

        ``submitted_by`` is the Clerk member who sent the first canonical-room
        message — not necessarily ``user_id`` (the owner row's PK, which is
        the org creator for an org). Any room member can supply that first
        message. For a personal owner ``submitted_by == user_id``.

        ``brief_text`` is the user's own words for what they want built
        (normally the captured first message), stamped onto
        ``onboarding_brief_text`` on the same CAS write so the staged
        ``BRIEF.md`` has the user's own words. ``None`` simply leaves the
        attribute unset — the SET clause omits it entirely; the condition fence
        (generation + ``not_started``) is UNCHANGED either way, so the added
        field can never weaken the claim's atomicity.

        Callers MUST ``revert_funnel_submission`` on a later failure (before
        ``record_onboarding_ids`` lands) so a retry isn't permanently locked
        out -- mirrors the old ``release_onboarding_claim`` contract.
        """
        table = self._table()
        # Bounded retry loop: the CAS itself is the serialization point, but a
        # peer can WIN the claim and then ``revert_funnel_submission`` it back
        # to ``not_started`` on the SAME generation if its downstream seed
        # fails. A single re-read that only distinguished generation-mismatch
        # from "not not_started" would then report ALREADY_SUBMITTED to a
        # racing caller even though the row is claimable again -- leaving the
        # client believing onboarding is done while the company has no
        # goal/project and no recovery path. So on a CAS failure we re-read
        # and, if the row is back at ``not_started`` on our generation, RETRY
        # the claim instead of mislabeling it. Bounded so a pathologically
        # flapping peer can't spin here forever.
        # The brief-text write is folded into the SAME SET clause as the claim
        # so it lands atomically with (and only with) a winning claim -- never a
        # separate write that could persist a brief for a claim that lost. When
        # absent ("you pick" path), the attribute is simply not set; the
        # ConditionExpression fence below is byte-for-byte unchanged in both
        # cases, so the added field cannot weaken the claim's atomicity.
        brief = (brief_text or "").strip()
        brief_set = ", onboarding_brief_text = :brief" if brief else ""
        for _attempt in range(3):
            now = utc_now_iso()
            values: dict = {
                ":submitted": "submitted",
                ":not_started": "not_started",
                ":gen": onboarding_generation,
                ":sub": submitted_by,
                ":t": now,
                ":u": now,
            }
            if brief:
                values[":brief"] = brief
            try:
                await run_in_thread(
                    table.update_item,
                    Key={"user_id": user_id},
                    UpdateExpression=(
                        "SET funnel_state = :submitted, funnel_submitted_at = :t, "
                        "onboarding_submitted_by = :sub, updated_at = :u" + brief_set
                    ),
                    ConditionExpression="onboarding_generation = :gen AND funnel_state = :not_started",
                    ExpressionAttributeValues=values,
                )
                publish_status_changed(owner_id or user_id)
                return FunnelClaimResult.WON
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                pass

            current = await self.get(user_id, consistent_read=True)
            if current is None or current.onboarding_generation != onboarding_generation:
                # Superseded generation (or the row vanished) -- per INV-4 a
                # silent no-op from the caller's point of view; the caller was
                # holding a stale view of the company, not colliding with a
                # duplicate submit.
                return FunnelClaimResult.STALE_GENERATION
            if current.funnel_state == "not_started":
                # A peer won then reverted (failed seed): the row is claimable
                # again on this same generation. Retry rather than reporting a
                # false ALREADY_SUBMITTED.
                continue
            # funnel_state is submitted/complete on our generation -- a peer's
            # submission genuinely won and is still standing.
            return FunnelClaimResult.ALREADY_SUBMITTED

        # Exhausted retries -- a peer is actively claim/revert-thrashing (its
        # seed keeps failing). Report STALE_GENERATION so the caller retries
        # (409 -> re-poll) rather than a false ALREADY_SUBMITTED that would
        # strand the client on an empty company.
        return FunnelClaimResult.STALE_GENERATION

    async def record_onboarding_ids(
        self,
        user_id: str,
        *,
        onboarding_generation: str,
        goal_id: str,
        project_id: str,
        owner_id: Optional[str] = None,
    ) -> None:
        """Record the seeded goal/project ids on the row. Written LAST (same
        ordering rationale as the old sentinel's ``record_onboarding_ids``):
        the initial goal and project must be linked before fulfilment stages
        the dispatched agent's ``BRIEF.md``.

        Guarded on ``onboarding_generation = :gen AND funnel_state =
        :submitted`` -- this can only ever land against the SAME claim this
        caller just won via ``claim_funnel_submission``; a company that has
        since been reprovisioned (new generation) or whose funnel somehow
        moved on cannot be clobbered by a stale caller. Raises
        ``ConditionalCheckFailedException`` on a guard failure, propagating
        through the fulfiller's recovery path.
        """
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET onboarding_goal_id = :g, onboarding_project_id = :p, updated_at = :u",
            ConditionExpression="onboarding_generation = :gen AND funnel_state = :submitted",
            ExpressionAttributeValues={
                ":g": goal_id,
                ":p": project_id,
                ":u": utc_now_iso(),
                ":gen": onboarding_generation,
                ":submitted": "submitted",
            },
        )
        # These ids are the LAST onboarding fields to land, and compute_status
        # returns them. claim_funnel_submission already nudged on funnel_state ->
        # submitted, but a tab that revalidated off that early event read a
        # contract missing goal/project; without this second nudge nothing
        # repairs it promptly (ready-state polling is no longer frequent). See
        # publish_status_changed's module docstring (INV-1: nudge only).
        publish_status_changed(owner_id or user_id)

    async def revert_funnel_submission(
        self, user_id: str, *, onboarding_generation: str, owner_id: Optional[str] = None
    ) -> None:
        """Best-effort rollback: ``funnel_state: submitted -> not_started``,
        so a caller that won the claim but then failed mid-seed doesn't
        permanently lock the company out of onboarding (mirrors the old
        sentinel's ``release_onboarding_claim``).

        Guarded on the SAME (generation, submitted) pair
        ``claim_funnel_submission`` fenced on, so this can only ever undo
        THIS caller's own still-standing claim -- never a since-reprovisioned
        company's fresh generation, and never a funnel a concurrent process
        has already advanced to ``complete``. A lost guard
        (``ConditionalCheckFailedException``) is swallowed, not raised: the
        claim is either already reverted, already advanced past
        ``submitted``, or the company was reprovisioned out from under it --
        none of which this caller should touch.
        """
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression=(
                    "SET funnel_state = :not_started, updated_at = :u "
                    "REMOVE funnel_submitted_at, onboarding_goal_id, onboarding_project_id, "
                    "onboarding_submitted_by, onboarding_brief_text"
                ),
                ConditionExpression="onboarding_generation = :gen AND funnel_state = :submitted",
                ExpressionAttributeValues={
                    ":not_started": "not_started",
                    ":gen": onboarding_generation,
                    ":submitted": "submitted",
                    ":u": utc_now_iso(),
                },
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return
        # The revert moved funnel_state back to not_started and dropped the
        # goal/project ids -- all facts compute_status composes. Nudge so a tab
        # that saw the optimistic "submitted" doesn't sit on a stale contract
        # after the seed failed and rolled back. Only reached when the write
        # actually landed (the swallowed guard-failure returns above).
        publish_status_changed(owner_id or user_id)

    async def complete_funnel_submission(
        self, user_id: str, *, onboarding_generation: str, owner_id: Optional[str] = None
    ) -> bool:
        """Complete the generation after its goal, project, and BRIEF.md exist."""
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression="SET funnel_state = :complete, funnel_completed_at = :t, updated_at = :t",
                ConditionExpression=(
                    "onboarding_generation = :gen AND funnel_state = :submitted "
                    "AND attribute_exists(onboarding_goal_id) AND attribute_exists(onboarding_project_id)"
                ),
                ExpressionAttributeValues={
                    ":complete": "complete",
                    ":submitted": "submitted",
                    ":gen": onboarding_generation,
                    ":t": utc_now_iso(),
                },
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
        publish_status_changed(owner_id or user_id)
        return True

    # --- Pending brief (funnel intake before the workspace can receive it) ---
    #
    # The first canonical-room human message is intake: it records what the
    # user said, then normal dispatch continues. Everything that needs
    # Paperclip to be up (the claim, goal/project seed, and BRIEF.md staging)
    # happens in ``onboarding_brief.reconcile_pending_brief``. These methods
    # are that hand-off's storage.
    #
    # The message can arrive while a just-created workspace is still settling,
    # so the durable item lets fulfilment retry without relying on a browser
    # tab remaining open.
    #
    # ``brief_state`` is the item's own lifecycle, deliberately separate from
    # the row's ``funnel_state``:
    #
    #   pending -> (drained)   item deleted by the drain that WON the claim
    #           -> failed      fulfilment failed too many times; the funnel is
    #                          re-shown so the user can submit again, and that
    #                          resubmit OVERWRITES this item (the conditional
    #                          on ``put_pending_brief`` admits a failed item).
    #
    # A ``failed`` item is deliberately kept rather than deleted: it carries
    # ``last_error`` for the next operator who asks why an owner never got
    # Friday, and its presence is what stops ``_has_pending_brief`` from
    # dismissing the funnel.
    #
    # LIFETIME: a pending item SURVIVES provisioning by design -- and, for the
    # same reason, survives a reprovision. An item can only still be here if
    # the brief was never fulfilled, and an owner who answered the funnel and
    # got nothing for it should have that answer carried into the company that
    # finally works. Teardown paths that delete an owner's company row must
    # delete this item too (see routers/debug.py) -- otherwise a wiped account
    # inherits the previous run's answer.

    async def put_pending_brief(
        self,
        owner_id: str,
        *,
        path: str,
        user_input: Optional[str],
        detail: Optional[str],
        brief_text: Optional[str],
        submitted_by: str,
        source_message_id: Optional[str] = None,
        target_agent_id: Optional[str] = None,
        overwrite: bool = False,
    ) -> bool:
        """Record a funnel answer that can't be fulfilled yet.

        Returns True iff this caller's answer landed; False when one is
        already pending -- a second org member submitting concurrently, or the
        same user double-clicking. That conditional is what makes intake
        one-shot per company WITHOUT needing the owner row to exist yet: the
        funnel claim (``claim_funnel_submission``) is still the authority once
        there IS a row, and this is the same guarantee projected back onto the
        window before it.

        A ``failed`` item is NOT protected by that conditional: fulfilment
        gave up, the funnel has been re-shown, and this submit is the user
        answering it again. Refusing here would leave them submitting into a
        void forever -- the funnel is a committed front door with no skip.

        ``brief_text`` is precomputed at intake (rather than at drain time) so
        the phrasing the user submitted is what the dispatched agent opens with,
        even if the brief-building code changes between the two.

        ``target_agent_id`` comes from the durable message's normal dispatch
        result; fulfilment must not independently select or force an agent.

        ``overwrite=True`` skips the put-if-absent guard entirely and
        replaces whatever is there, ``pending`` included -- for a
        pre-container "edit your idea" re-submit, which a caller may only take
        for an owner it has confirmed has no container row yet, so there is no
        in-flight fulfilment claim on this item to clobber. (The landing card
        that used it went away with routers/onboarding.py; the guard stays
        because the put-if-absent semantics below depend on it.) Never pass
        this for a container-having owner.
        """
        item = {
            "user_id": _pending_brief_key(owner_id),
            "kind": "pending_brief",
            "brief_state": "pending",
            "attempts": 0,
            "owner_id": owner_id,
            "path": path,
            "submitted_by": submitted_by,
            "submitted_at": utc_now_iso(),
        }
        # Absent, not "": "no idea supplied" is a real signal on the "you pick"
        # path, and it must survive the round trip as None rather than as a
        # blank string a later reader would quote back at the dispatched agent.
        if user_input:
            item["input"] = user_input
        if detail:
            item["detail"] = detail
        if brief_text:
            item["brief_text"] = brief_text
        if source_message_id:
            item["source_message_id"] = source_message_id
        if target_agent_id:
            item["target_agent_id"] = target_agent_id

        table = self._table()
        if overwrite:
            await run_in_thread(table.put_item, Item=item)
            return True
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="attribute_not_exists(user_id) OR brief_state = :failed",
                ExpressionAttributeValues={":failed": "failed"},
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def _has_pending_brief(self, owner_id: str) -> bool:
        """Cheap existence check for ``get_onboarding_state``'s additive
        ``pending_brief`` key. True only for an item still worth draining.

        DELIBERATELY additive rather than folded into ``funnel_state``. The
        same dict is consumed by ``compute_status`` (which SHOULD treat a
        pending answer as "funnel submitted" -- the user answered, so stop
        reporting a submitted onboarding state) and by the fulfiller (which
        must not mark onboarding complete before the goal/project exist).
        Reporting a synthetic ``funnel_state: "submitted"`` here would serve
        the first and break the second; the composer is where those facts are
        combined into a verdict.

        A ``failed`` item reports False on purpose: fulfilment gave up, so the
        funnel must come BACK and let the user submit again. Suppressing it
        would leave them in an empty chat with an agent that never starts and
        no way to retry.

        A read failure degrades to False rather than raising, matching this
        accessor's contract (its caller is the ``/container/status`` poll):
        the cost is one more poll showing the funnel, and the next drain
        still fulfils the brief.
        """
        try:
            item = await self.get_pending_brief(owner_id)
        except Exception:  # noqa: BLE001 — a degraded read must not break /status
            logger.info("paperclip_repo: pending-brief read failed for owner %s", owner_id, exc_info=True)
            return False
        return item is not None and item.get("brief_state") != "failed"

    async def get_pending_brief(self, owner_id: str) -> Optional[dict]:
        """The pending funnel answer for this owner, or None.

        Strongly consistent: the drain reads this and then conditionally
        writes the owner row off what it finds, and a status poll that missed
        a just-written item would re-show the funnel to someone who already
        answered it.
        """
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": _pending_brief_key(owner_id)},
            ConsistentRead=True,
        )
        return resp.get("Item")

    async def record_pending_brief_failure(self, owner_id: str, *, reason: str) -> int:
        """Count one FAILED fulfilment attempt; return the new total.

        Only real failures are counted -- "the workspace isn't ready yet" is
        the normal first-run state and must never burn an attempt, or a slow
        provision would exhaust the budget before it could possibly succeed.

        The counter is on the item rather than in memory because the process
        that fails is often not the process that retries (a rolling deploy, a
        different ECS task picking up the next poll).

        Conditional on the item existing: an unconditional update_item against
        a concurrently-deleted item would RESURRECT it as a partial (attempts
        + last_error, no brief_state), which ``put_pending_brief``'s guard
        then rejects forever. Item gone means nothing to count -- report 0.
        """
        table = self._table()
        try:
            resp = await run_in_thread(
                table.update_item,
                Key={"user_id": _pending_brief_key(owner_id)},
                UpdateExpression="ADD attempts :one SET last_error = :e, last_attempt_at = :t",
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues={
                    ":one": 1,
                    ":e": reason[:500],
                    ":t": utc_now_iso(),
                },
                ReturnValues="UPDATED_NEW",
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return 0
        return int(resp.get("Attributes", {}).get("attempts", 0))

    async def fail_pending_brief(self, owner_id: str, *, reason: str, owner_broadcast_id: Optional[str] = None) -> None:
        """Give up on fulfilling this brief: ``brief_state -> failed``.

        This is what ENDS the retry loop, and it is deliberately visible to
        the user rather than silent: ``_has_pending_brief`` stops reporting a
        failed item, so ``compute_status`` stops synthesising
        ``funnel_state: "submitted"`` and the funnel comes back. The user can
        then submit again (``put_pending_brief`` overwrites a failed item).
        Without this a deterministic fulfilment failure left them in an empty
        chat, with an agent that never starts and no way to retry.

        Conditional on the item existing, for the same reason as
        ``record_pending_brief_failure``: resurrecting a deleted item as a
        partial would block every future submit. Gone item = nothing to mark.
        """
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": _pending_brief_key(owner_id)},
                UpdateExpression="SET brief_state = :failed, last_error = :e, failed_at = :t",
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues={
                    ":failed": "failed",
                    ":e": reason[:500],
                    ":t": utc_now_iso(),
                },
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return
        # The funnel is about to re-render for every tab on this company.
        publish_status_changed(owner_broadcast_id or owner_id)

    async def delete_pending_brief(self, owner_id: str) -> bool:
        """Retire a pending answer once it has demonstrably been fulfilled.
        Returns whether an item was actually there.

        Callers must be sure fulfilment actually happened -- see
        ``reconcile_pending_brief``, where only the drain that WON the claim
        deletes on its own success, and a drain that lost deletes only on the
        one unambiguous end-state (``funnel_state == "complete"``, after the
        initial goal/project and BRIEF.md stage). Deleting on weaker evidence
        loses the brief in exactly the case this mechanism exists to survive:
        the winner can still fail and roll everything back.

        The return value exists for callers that must attempt the delete
        UNCONDITIONALLY (``core.services.account_wipe``, whose existence
        snapshot is read before the item it is deleting could have been
        written) and still report what they actually removed.
        """
        resp = await run_in_thread(
            self._table().delete_item,
            Key={"user_id": _pending_brief_key(owner_id)},
            ReturnValues="ALL_OLD",
        )
        return bool(resp.get("Attributes"))

    async def scan_purge_due(self, now: datetime) -> list[PaperclipCompany]:
        """Return disabled rows whose ``scheduled_purge_at <= now`` via the GSI.

        Paginates through ``LastEvaluatedKey`` so larger result sets past
        the DynamoDB single-page Query limit (~1MB) are not silently
        dropped (mirrors the pagination pattern in
        ``channel_link_repo.delete_all_for_owner``).
        """
        out: list[PaperclipCompany] = []
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-status-purge-at",
                "KeyConditionExpression": (
                    Key("status").eq("disabled") & Key("scheduled_purge_at").lte(now.isoformat())
                ),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            for item in resp.get("Items", []):
                company = await self.get(item["user_id"])
                if company is not None:
                    out.append(company)
                else:
                    logger.warning(
                        "paperclip_repo.scan_purge_due: GSI returned %s but base item missing",
                        item.get("user_id"),
                    )
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                return out
