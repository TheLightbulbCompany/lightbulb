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

from boto3.dynamodb.conditions import Key

from core.auth import is_org_owner_id
from core.dynamodb import get_table, run_in_thread
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
    sentinel's claim/steal vocabulary — see
    ``docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md``
    §2). The funnel claim is now a real, generation-fenced attribute
    transition on the owner row (``funnel_state: not_started -> submitted``)
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


PaperclipCompanyStatus = Literal["provisioning", "active", "failed", "disabled"]
FunnelState = Literal["not_started", "submitted", "complete"]
BootstrapState = Literal["not_started", "dispatched", "delivered", "failed"]


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
    last_error: Optional[str] = None
    scheduled_purge_at: Optional[datetime] = None
    seeded_agent_ids: Optional[list[str]] = None
    """Paperclip agent IDs of the Isol8-seeded agents (Friday + Atlas)
    captured at provision time. The stable marker for "this agent is part
    of the provisioning contract and must not be deleted" — used by
    ``paperclip_openclaw_pairing.is_seeded_agent`` and the Teams BFF
    ``seeded`` flag. Only the provisioner's row carries this (it's the
    row whose ``provision_org`` actually seeded the agents); members'
    rows leave it None. Matching on persisted IDs (rather than role/name)
    means a user-created agent that happens to use role ``ceo`` or the
    name ``Atlas`` is never mistaken for a seeded one, and renaming a
    seeded agent never drops its protection."""

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
    """``not_started`` (set at G2) -> ``submitted`` (``POST
    /onboarding/brief`` claims this, fenced on ``onboarding_generation``) ->
    ``complete`` (set once the bootstrap dispatcher observes Friday's
    kickoff ``chat.final`` — a later PR; NOT set by this one)."""

    funnel_submitted_at: Optional[datetime] = None
    funnel_completed_at: Optional[datetime] = None
    onboarding_goal_id: Optional[str] = None
    onboarding_project_id: Optional[str] = None

    bootstrap_state: Optional[BootstrapState] = None
    """``not_started`` (set at G2) -> ``dispatched`` -> ``delivered`` |
    ``failed``. Written by the bootstrap dispatcher (a later PR) — this PR
    only establishes the attribute and its G2-time initial value."""

    bootstrap_dispatch_id: Optional[str] = None
    bootstrap_dispatched_at: Optional[datetime] = None
    bootstrap_delivered_at: Optional[datetime] = None
    bootstrap_attempts: Optional[int] = None
    bootstrap_last_error: Optional[str] = None


class PaperclipRepo:
    """Async repository for the paperclip-companies DynamoDB table.

    The table name is injected so the same repo class is usable for the
    real per-environment table (e.g. ``isol8-dev-paperclip-companies``)
    and for moto-backed unit tests. Internally we resolve the boto3
    Table via ``core.dynamodb.get_table`` so prefix-patching in tests
    (and the LocalStack endpoint override) Just Work.
    """

    def __init__(self, table_name: str, region: str | None = None):  # noqa: ARG002 - region kept for back-compat
        # ``region`` is accepted for back-compat with existing call sites
        # but ignored: the shared boto3 resource in core.dynamodb already
        # handles region + endpoint configuration.
        self._table_name = table_name

    def _table(self):
        return get_table(self._table_name)

    async def put(self, company: PaperclipCompany) -> None:
        """Whole-item put — replaces the row entirely.

        ⚠️ WRITER-SAFETY (see the architecture spec's "load-bearing safety
        requirement", §2): this is a raw ``PutItem`` — any onboarding field
        NOT set on ``company`` is simply ABSENT from the new item, which
        silently WIPES a previously-written value (``onboarding_generation``,
        ``funnel_state``, ``bootstrap_state``, ...). Every caller of ``put``
        MUST carry forward the row's existing onboarding fields (read-merge)
        unless it is deliberately establishing them fresh (the G2 mint site
        in ``paperclip_provisioning.provision_org``). Prefer the
        generation-conditioned ``UpdateItem`` methods below
        (``claim_funnel_submission`` / ``record_onboarding_ids`` /
        ``revert_funnel_submission``) for any onboarding-only write — they
        cannot clobber unrelated fields by construction.
        """
        item = {
            "user_id": company.user_id,
            "org_id": company.org_id,
            "company_id": company.company_id,
            "paperclip_user_id": company.paperclip_user_id,
            "paperclip_password_encrypted": company.paperclip_password_encrypted,
            "service_token_encrypted": company.service_token_encrypted,
            "status": company.status,
            "created_at": company.created_at.isoformat(),
            "updated_at": company.updated_at.isoformat(),
        }
        if company.last_error is not None:
            item["last_error"] = company.last_error
        if company.scheduled_purge_at is not None:
            item["scheduled_purge_at"] = company.scheduled_purge_at.isoformat()
        if company.seeded_agent_ids:
            item["seeded_agent_ids"] = company.seeded_agent_ids
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
        if company.bootstrap_state is not None:
            item["bootstrap_state"] = company.bootstrap_state
        if company.bootstrap_dispatch_id is not None:
            item["bootstrap_dispatch_id"] = company.bootstrap_dispatch_id
        if company.bootstrap_dispatched_at is not None:
            item["bootstrap_dispatched_at"] = company.bootstrap_dispatched_at.isoformat()
        if company.bootstrap_delivered_at is not None:
            item["bootstrap_delivered_at"] = company.bootstrap_delivered_at.isoformat()
        if company.bootstrap_attempts is not None:
            item["bootstrap_attempts"] = company.bootstrap_attempts
        if company.bootstrap_last_error is not None:
            item["bootstrap_last_error"] = company.bootstrap_last_error
        await run_in_thread(self._table().put_item, Item=item)
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
            company_id=item["company_id"],
            paperclip_user_id=item.get("paperclip_user_id", ""),
            paperclip_password_encrypted=item.get("paperclip_password_encrypted", ""),
            service_token_encrypted=item.get("service_token_encrypted", ""),
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            last_error=item.get("last_error"),
            scheduled_purge_at=(
                datetime.fromisoformat(item["scheduled_purge_at"]) if item.get("scheduled_purge_at") else None
            ),
            seeded_agent_ids=(list(item["seeded_agent_ids"]) if item.get("seeded_agent_ids") else None),
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
            bootstrap_state=item.get("bootstrap_state"),
            bootstrap_dispatch_id=item.get("bootstrap_dispatch_id"),
            bootstrap_dispatched_at=(
                datetime.fromisoformat(item["bootstrap_dispatched_at"]) if item.get("bootstrap_dispatched_at") else None
            ),
            bootstrap_delivered_at=(
                datetime.fromisoformat(item["bootstrap_delivered_at"]) if item.get("bootstrap_delivered_at") else None
            ),
            bootstrap_attempts=(
                int(item["bootstrap_attempts"]) if item.get("bootstrap_attempts") is not None else None
            ),
            bootstrap_last_error=item.get("bootstrap_last_error"),
        )

    async def get_onboarding_state(self, owner_id: str) -> dict:
        """RECONCILE-WITH-ONBOARD: read-only accessor for the
        COMPOSER/ONBOARD interface contract.

        Returns the 11 spec-fixed onboarding keys (see the dataclass note
        above) PLUS three additive keys -- ``teams_state`` (this row's raw
        ``status``), ``company_id``, and ``paperclip_user_id`` -- so
        ``core.services.readiness_state.compute_status`` gets everything it
        needs from the paperclip-companies row in exactly ONE physical read
        rather than a second `get_item` just for onboarding fields. See
        docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md
        §2 ("one read, not two").

        ``owner_id`` is the CONTAINER owner (org_id for an org, else the
        user_id) -- NOT necessarily a row key. This table's PK is a Clerk
        user_id (see module docstring): for a personal owner
        (user_id == owner_id) ``self.get(owner_id)`` already resolves the
        right row directly (the common case, ONE physical read). For an
        ORG owner (``owner_id`` is an ``org_`` id) there is no row keyed by
        org_id itself, so this falls back to the ``by-org-id`` GSI (also
        ONE physical read -- ``ProjectionType=ALL``, no base-table chain
        read needed) and returns the PROVISIONER's row -- the one row
        ``seeded_agent_ids`` was stamped on at provision time -- since
        onboarding facts (funnel/bootstrap) are company-wide, not
        per-member. Falls back to the first row if none carries
        ``seeded_agent_ids`` (defensive; shouldn't happen for an
        actively-provisioned org).

        Owner-row resolution is delegated to ``resolve_owner_row`` -- the
        single canonical "which row is THE owner row" path shared with
        ``routers/onboarding.py`` and ``routers/container.py`` -- so org vs.
        personal resolution has exactly one implementation.

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
            return {
                "onboarding_generation": None,
                "funnel_state": "not_started",
                "funnel_submitted_at": None,
                "funnel_completed_at": None,
                "onboarding_goal_id": None,
                "onboarding_project_id": None,
                "bootstrap_state": "not_started",
                "bootstrap_dispatch_id": None,
                "bootstrap_dispatched_at": None,
                "bootstrap_delivered_at": None,
                "bootstrap_attempts": 0,
                "bootstrap_last_error": None,
                "teams_state": None,
                "company_id": None,
                "paperclip_user_id": None,
            }

        # getattr(..., default) rather than direct attribute access: real
        # PaperclipCompany instances always have every field (defaulted by
        # the dataclass), but a handful of existing test doubles construct
        # a bare SimpleNamespace(status=...) standing in for `repo.get`'s
        # return value -- getattr keeps this method correct against both
        # without forcing every such test double to be rewritten into a
        # full PaperclipCompany just to add a status field.
        # ONBOARD's dataclass carries funnel/bootstrap timestamps as
        # ``datetime`` and defaults the state literals to ``None`` (not the
        # "not_started" string). This accessor is the COMPOSER/ONBOARD bridge:
        # it normalizes those to the contract's string-typed shape -- ISO
        # strings for timestamps, "not_started" for an unset state -- so
        # ``compute_status`` and the StatusContract never see a bare datetime
        # or a null state.
        def _iso(value):
            return value.isoformat() if hasattr(value, "isoformat") else value

        return {
            "onboarding_generation": getattr(company, "onboarding_generation", None),
            "funnel_state": getattr(company, "funnel_state", None) or "not_started",
            "funnel_submitted_at": _iso(getattr(company, "funnel_submitted_at", None)),
            "funnel_completed_at": _iso(getattr(company, "funnel_completed_at", None)),
            "onboarding_goal_id": getattr(company, "onboarding_goal_id", None),
            "onboarding_project_id": getattr(company, "onboarding_project_id", None),
            "bootstrap_state": getattr(company, "bootstrap_state", None) or "not_started",
            "bootstrap_dispatch_id": getattr(company, "bootstrap_dispatch_id", None),
            "bootstrap_dispatched_at": _iso(getattr(company, "bootstrap_dispatched_at", None)),
            "bootstrap_delivered_at": _iso(getattr(company, "bootstrap_delivered_at", None)),
            "bootstrap_attempts": getattr(company, "bootstrap_attempts", None) or 0,
            "bootstrap_last_error": getattr(company, "bootstrap_last_error", None),
            "teams_state": getattr(company, "status", None),
            "company_id": getattr(company, "company_id", None),
            "paperclip_user_id": getattr(company, "paperclip_user_id", None),
        }

    async def get_org_company_id(self, org_id: str) -> Optional[str]:
        """Return the shared ``company_id`` for any one user in this org.

        Used at member-join time (``organizationMembership.created``):
        if any other user in the org already has a row, we reuse their
        company_id rather than creating a new Paperclip company.

        Implementation: queries the ``by-org-id`` GSI (PK: ``org_id``).
        Returns the first row's ``company_id``. All rows in the same
        org are required (by the provisioner) to carry the same
        ``company_id``, so picking any one is correct.

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
        kwargs = {
            "IndexName": "by-org-id",
            "KeyConditionExpression": Key("org_id").eq(org_id),
            "Limit": 1,
        }
        resp = await run_in_thread(self._table().query, **kwargs)
        items = resp.get("Items", [])
        if not items:
            return None
        first = items[0]
        # If the GSI projection only includes keys, fall back to a base
        # get to retrieve company_id. With ProjectionType=ALL this
        # branch is a no-op.
        if "company_id" in first:
            return first["company_id"]
        base = await self.get(first["user_id"])
        return base.company_id if base is not None else None

    async def list_by_org_id(self, org_id: str) -> list[dict]:
        """Return all rows in ``org_id`` via the ``by-org-id`` GSI.

        Returns raw DDB items (dicts) rather than ``PaperclipCompany``
        instances because callers (notably the Teams BFF members
        endpoint) typically only need a small subset of fields
        (``user_id``, ``paperclip_user_id``) to build a Paperclip→Clerk
        principal map. The GSI is declared with ``ProjectionType=ALL``
        in ``database-stack.ts`` so every field is available without a
        base-table chain read.

        Paginates ``LastEvaluatedKey`` to handle orgs larger than a
        single Query page; for typical Isol8 org sizes (1-10 users)
        one page suffices.
        """
        items: list[dict] = []
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            items.extend(resp.get("Items", []))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
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
        returns just the count without materializing items. We
        paginate ``LastEvaluatedKey`` because Query's per-page count
        cap (~1MB or 1000 items) would otherwise undercount large orgs.
        For typical Isol8 org sizes (1-10 users) one page suffices.
        """
        count = 0
        last_evaluated_key: dict | None = None
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
                "Select": "COUNT",
            }
            if last_evaluated_key is not None:
                kwargs["ExclusiveStartKey"] = last_evaluated_key
            resp = await run_in_thread(self._table().query, **kwargs)
            count += int(resp.get("Count", 0))
            last_evaluated_key = resp.get("LastEvaluatedKey")
            if not last_evaluated_key:
                return count

    async def update_status(
        self,
        user_id: str,
        *,
        status: PaperclipCompanyStatus,
        last_error: Optional[str] = None,
        scheduled_purge_at: Optional[datetime] = None,
        not_if_status: Optional[PaperclipCompanyStatus] = None,
        owner_id: Optional[str] = None,
    ) -> bool:
        """Update a row's status. Returns True iff the write landed.

        ``not_if_status`` guards the write with a ConditionExpression so the
        update is SKIPPED (returns False) when the row is currently in that
        status. Used so a losing concurrent provision attempt can't downgrade
        an already-``active`` row to ``failed``: two overlapping ``provision_member``
        attempts for one member can race (e.g. /teams + /chat both reconciling),
        and the loser's failure path must not clobber the winner's active row.

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
            ":u": datetime.now(timezone.utc).isoformat(),
        }
        if last_error is not None:
            update_expr += ", last_error = :e"
            expr_values[":e"] = last_error
        if scheduled_purge_at is not None:
            update_expr += ", scheduled_purge_at = :p"
            expr_values[":p"] = scheduled_purge_at.isoformat()

        kwargs: dict = {
            "Key": {"user_id": user_id},
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": expr_names,
            "ExpressionAttributeValues": expr_values,
        }
        if not_if_status is not None:
            # #s already maps to "status"; reuse it for the guard. Also require
            # the row to still exist (a deleted row shouldn't be re-created here).
            kwargs["ConditionExpression"] = "attribute_exists(user_id) AND #s <> :guard"
            expr_values[":guard"] = not_if_status

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
        now = datetime.now(timezone.utc).isoformat()
        item = {
            "user_id": user_id,
            "org_id": org_id,
            "company_id": company_id,
            "status": "failed",
            "last_error": reason,
            "created_at": now,
            "updated_at": now,
        }
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
    ) -> bool:
        """Strongly-consistent "I am creating this org's company" claim.

        Returns True iff THIS caller now holds the claim — by winning a fresh
        claim, or by stealing one whose holder went silent for longer than
        ``stale_after_seconds``. Returns False when another caller holds a
        non-stale claim.

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
        item = {
            "user_id": key,
            "kind": "org_create_claim",
            "holder": holder,
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
            return True
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
                return True
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                return False

        prev_claimed_at = existing.get("claimed_at", "")
        try:
            age = (now - datetime.fromisoformat(prev_claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")  # unparseable timestamp → treat as stale
        if age < stale_after_seconds:
            return False

        # Steal — overwrite only if claimed_at is unchanged since our read, so
        # two concurrent stealers can't both win.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="claimed_at = :prev",
                ExpressionAttributeValues={":prev": prev_claimed_at},
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def release_org_create_claim(self, org_id: str, *, holder: str) -> None:
        """Delete the per-org create claim iff we still hold it.

        Called on create FAILURE so the next poll can re-acquire and retry.
        Guarded on ``holder`` so we never delete a claim another caller has
        since stolen. Best-effort: a lost guard is a no-op.
        """
        key = _org_create_claim_key(org_id)
        table = self._table()
        try:
            await run_in_thread(
                table.delete_item,
                Key={"user_id": key},
                ConditionExpression="holder = :h",
                ExpressionAttributeValues={":h": holder},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    async def find_owner_row(self, org_id: str, *, include_inactive: bool = False) -> Optional[PaperclipCompany]:
        """Find the ORG owner row (oldest ACTIVE ``created_at`` for this org_id).

        Deliberately a separate, repo-level implementation of the same
        query ``PaperclipProvisioning.find_org_owner`` runs (rather than a
        refactor of that tested, load-bearing method) -- see
        ``resolve_owner_row`` for why the repo needs this independently of
        provisioning: onboarding reads/writes (``routers/onboarding.py``,
        ``routers/container.py``) need to resolve an ``owner_id`` to the row
        that carries onboarding truth WITHOUT going through the
        provisioning orchestrator.

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
            chosen = min(active_items, key=_sort_key)
        elif include_inactive and items:
            # No active row -- return the provisioner's row (carries
            # seeded_agent_ids) if present, else the oldest, so a failed-seed
            # org still resolves to a row carrying its onboarding/teams truth.
            chosen = next((it for it in items if it.get("seeded_agent_ids")), None) or min(items, key=_sort_key)
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
        (``routers/onboarding.py``, ``routers/container.py``'s ``onboarded``
        computation) should use, so "which row is THE owner row" has one
        answer.

        ``consistent_read`` defaults to False (eventually-consistent). The
        HOT caller is ``get_onboarding_state`` on every ``/container/status``
        poll (~3s/owner), where ``onboarded`` is best-effort and a briefly
        stale ``False`` is harmless -- a strong read there would double the
        ``paperclip-companies`` RCU cost fleet-wide for a field that flips
        once. The one caller that needs strong consistency is
        ``POST /onboarding/brief`` (the funnel claim reads then writes the
        SAME row); it passes ``consistent_read=True``. NOTE: the org path
        goes through the ``by-org-id`` GSI, which is eventually consistent by
        construction -- ``consistent_read`` only strengthens the personal
        (base-table ``get``) path.
        """
        if is_org_owner_id(owner_id):
            return await self.find_owner_row(owner_id, include_inactive=include_inactive)
        return await self.get(owner_id, consistent_read=consistent_read)

    async def claim_funnel_submission(
        self, user_id: str, *, onboarding_generation: str, owner_id: Optional[str] = None
    ) -> FunnelClaimResult:
        """Conditional ``funnel_state: not_started -> submitted``, fenced on
        ``onboarding_generation`` (INV-4). Replaces the ``__onboarded__``
        sentinel's ``claim_onboarding`` -- see ``FunnelClaimResult`` for why
        this is now a single atomic UpdateItem on the row itself instead of
        a separate claim item with its own stale-steal timing: DynamoDB's
        conditional write already serializes concurrent submitters, and the
        two ways to lose (superseded generation vs. already submitted) are
        distinguished by one strongly-consistent re-read on failure.

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
        for _attempt in range(3):
            now = datetime.now(timezone.utc).isoformat()
            try:
                await run_in_thread(
                    table.update_item,
                    Key={"user_id": user_id},
                    UpdateExpression="SET funnel_state = :submitted, funnel_submitted_at = :t, updated_at = :u",
                    ConditionExpression="onboarding_generation = :gen AND funnel_state = :not_started",
                    ExpressionAttributeValues={
                        ":submitted": "submitted",
                        ":not_started": "not_started",
                        ":gen": onboarding_generation,
                        ":t": now,
                        ":u": now,
                    },
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
        BOOTSTRAP.md tells Friday to go to work, so both Paperclip rows must
        exist and be linked here before it lands, or she wakes into a
        company with no project.

        Guarded on ``onboarding_generation = :gen AND funnel_state =
        :submitted`` -- this can only ever land against the SAME claim this
        caller just won via ``claim_funnel_submission``; a company that has
        since been reprovisioned (new generation) or whose funnel somehow
        moved on cannot be clobbered by a stale caller. Raises
        ``ConditionalCheckFailedException`` on a guard failure, propagating
        through a generic handler to a raw 500 -- same contract as the old
        sentinel's ``record_onboarding_ids``, so the router's except block
        is unchanged.
        """
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET onboarding_goal_id = :g, onboarding_project_id = :p, updated_at = :u",
            ConditionExpression="onboarding_generation = :gen AND funnel_state = :submitted",
            ExpressionAttributeValues={
                ":g": goal_id,
                ":p": project_id,
                ":u": datetime.now(timezone.utc).isoformat(),
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
                    "REMOVE funnel_submitted_at, onboarding_goal_id, onboarding_project_id"
                ),
                ConditionExpression="onboarding_generation = :gen AND funnel_state = :submitted",
                ExpressionAttributeValues={
                    ":not_started": "not_started",
                    ":gen": onboarding_generation,
                    ":submitted": "submitted",
                    ":u": datetime.now(timezone.utc).isoformat(),
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
