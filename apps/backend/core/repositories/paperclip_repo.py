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

from core.dynamodb import get_table, run_in_thread

logger = logging.getLogger(__name__)


class OnboardingClaimResult(str, Enum):
    """What ``claim_onboarding`` decided, for the caller to act on.

    A bare bool conflates two very different "False" cases: "the company is
    genuinely done" (a durable, terminal fact worth returning to the client
    as success) and "another request holds a fresh in-flight claim" (a
    transient state that should be retried, not treated as done). Callers
    that can't tell those apart risk marking a caller's own submission
    ``already_submitted`` on a company that in fact has no goal or project
    yet, because the peer whose claim they collided with later failed.
    """

    WON = "won"
    """This caller now holds the claim (fresh or stolen) and should
    proceed to seed."""

    SEEDED = "seeded"
    """The existing claim already completed (``seeded_at`` is set). This is
    the ONLY case that means genuinely, durably done."""

    IN_FLIGHT = "in_flight"
    """A non-stale claim exists with no ``seeded_at`` yet -- someone else is
    legitimately working on it right now. Retry later; do not treat as
    done."""


PaperclipCompanyStatus = Literal["provisioning", "active", "failed", "disabled"]


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


# Reserved user_id namespace for the per-company "onboarding seed" sentinel.
# Same rationale as the org-create claim above: the table is keyed per-user,
# so a field on the submitting user's row would leave every other member of
# the same org unmarked. This synthetic PK is reached only by direct
# get/put/update, never via a GSI, and a PK conditional write is strongly
# consistent and cross-instance-safe.
_ONBOARDING_CLAIM_PREFIX = "__onboarded__:"


def _onboarding_claim_key(company_id: str) -> str:
    return f"{_ONBOARDING_CLAIM_PREFIX}{company_id}"


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

    # -------------------------------------------------------------------
    # RECONCILE-WITH-ONBOARD: onboarding fields co-located on this row.
    #
    # These 11 additive, defaulted fields mirror the attributes the sibling
    # ONBOARD PR (spec PR-4 / ONBOARD-TABLE session) is landing on this same
    # paperclip-companies row -- see
    # docs/superpowers/specs/2026-07-24-onboarding-architecture-design.md §2.
    # ONBOARD owns minting `onboarding_generation` (event G2, in
    # `provision_org`'s fresh-create branch) and every write to the fields
    # below; this PR (COMPOSER) only READS them, via `get_onboarding_state`.
    # Whichever of ONBOARD/COMPOSER merges second should reconcile this
    # dataclass against ONBOARD's real field set (names here are the
    # COMPOSER/ONBOARD interface contract's spec-fixed keys, so they should
    # already match) and delete this note.
    # -------------------------------------------------------------------
    onboarding_generation: Optional[str] = None
    funnel_state: str = "not_started"
    funnel_submitted_at: Optional[str] = None
    funnel_completed_at: Optional[str] = None
    onboarding_goal_id: Optional[str] = None
    onboarding_project_id: Optional[str] = None
    bootstrap_state: str = "not_started"
    bootstrap_dispatch_id: Optional[str] = None
    bootstrap_dispatched_at: Optional[str] = None
    bootstrap_delivered_at: Optional[str] = None
    bootstrap_attempts: int = 0
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
        await run_in_thread(self._table().put_item, Item=item)

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
            # RECONCILE-WITH-ONBOARD: read-only, defaulted -- a row written
            # before ONBOARD's writers land (or a row ONBOARD never touched,
            # e.g. a member row) simply has none of these attributes, and
            # every default below is the "genuinely never started" value.
            onboarding_generation=item.get("onboarding_generation"),
            funnel_state=item.get("funnel_state", "not_started"),
            funnel_submitted_at=item.get("funnel_submitted_at"),
            funnel_completed_at=item.get("funnel_completed_at"),
            onboarding_goal_id=item.get("onboarding_goal_id"),
            onboarding_project_id=item.get("onboarding_project_id"),
            bootstrap_state=item.get("bootstrap_state", "not_started"),
            bootstrap_dispatch_id=item.get("bootstrap_dispatch_id"),
            bootstrap_dispatched_at=item.get("bootstrap_dispatched_at"),
            bootstrap_delivered_at=item.get("bootstrap_delivered_at"),
            bootstrap_attempts=int(item.get("bootstrap_attempts", 0)),
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

        RECONCILE-WITH-ONBOARD: if ONBOARD's real implementation resolves
        the owner row differently (e.g. a dedicated org_id-keyed
        attribute/table), align this against it.

        A row that doesn't exist yet, or that predates ONBOARD's writers,
        returns every onboarding field at its "genuinely not started"
        default -- never raises (read failures are the caller's
        responsibility to guard, matching every other method here).
        """
        company: Optional[PaperclipCompany]
        if owner_id.startswith("org_"):
            rows = await self.list_by_org_id(owner_id)
            item = next((r for r in rows if r.get("seeded_agent_ids")), None) or (rows[0] if rows else None)
            company = self._item_to_company(item) if item is not None else None
        else:
            company = await self.get(owner_id)
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
        return {
            "onboarding_generation": getattr(company, "onboarding_generation", None),
            "funnel_state": getattr(company, "funnel_state", "not_started"),
            "funnel_submitted_at": getattr(company, "funnel_submitted_at", None),
            "funnel_completed_at": getattr(company, "funnel_completed_at", None),
            "onboarding_goal_id": getattr(company, "onboarding_goal_id", None),
            "onboarding_project_id": getattr(company, "onboarding_project_id", None),
            "bootstrap_state": getattr(company, "bootstrap_state", "not_started"),
            "bootstrap_dispatch_id": getattr(company, "bootstrap_dispatch_id", None),
            "bootstrap_dispatched_at": getattr(company, "bootstrap_dispatched_at", None),
            "bootstrap_delivered_at": getattr(company, "bootstrap_delivered_at", None),
            "bootstrap_attempts": getattr(company, "bootstrap_attempts", 0),
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
    ) -> bool:
        """Update a row's status. Returns True iff the write landed.

        ``not_if_status`` guards the write with a ConditionExpression so the
        update is SKIPPED (returns False) when the row is currently in that
        status. Used so a losing concurrent provision attempt can't downgrade
        an already-``active`` row to ``failed``: two overlapping ``provision_member``
        attempts for one member can race (e.g. /teams + /chat both reconciling),
        and the loser's failure path must not clobber the winner's active row.
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

    async def claim_onboarding(
        self, company_id: str, *, holder: str, stale_after_seconds: int = 60
    ) -> OnboardingClaimResult:
        """Claim the one-shot onboarding seed for a COMPANY.

        The table is keyed per-user, so a field on the submitting user's
        row would leave every other member of the same org unmarked. This
        sentinel is keyed on company_id instead, following the same shape
        as the org-create claim above.

        PK conditional write: strongly consistent and cross-instance-safe,
        so two tabs submitting the brief at once cannot both seed. Exactly
        one gets ``WON`` on a fresh claim.

        On a collision (a sentinel already exists) this reads it via
        ``get_onboarding`` and decides:

        * ``seeded_at`` present -> the seed genuinely completed ->
          ``SEEDED``. A finished seed is never stealable, no matter how
          old. This is the ONLY result that means the company is durably
          done — callers must treat it, and only it, as
          ``already_submitted``.
        * no ``seeded_at`` and ``claimed_at`` is older than
          ``stale_after_seconds`` -> stranded (the holder died between
          claiming and recording -- e.g. an ECS task kill or a deploy
          landing in that ~3s window, which ``release_onboarding_claim``
          never runs for) -> take it over (becoming the new holder) and
          return ``WON``.
        * otherwise -> a claim is legitimately in flight -> ``IN_FLIGHT``.
          This is transient, not done: the holder may itself still fail
          and release, at which point a retry should be allowed to claim.

        Returning a bare bool here would conflate ``SEEDED`` and
        ``IN_FLIGHT`` into a single "False" — exactly the defect this
        result type exists to prevent (see ``OnboardingClaimResult``'s
        docstring): a caller that treats a losing collision as
        ``already_submitted`` regardless of WHY can mark itself done on a
        company that in fact has no goal or project, if the peer it
        collided with later fails.

        Without the stale-steal takeover, a stranded sentinel locks the
        company out of onboarding forever: every later submit sees
        ``SEEDED`` (or, before this fix, an undifferentiated
        ``already_submitted``) and the user is waved into a company with
        no goal, no project, and no first-run instructions staged, with no
        self-service recovery.

        Mirrors ``try_acquire_org_create_claim``'s structure: a fresh
        conditional put, then a strongly-consistent read, then a guarded
        steal keyed on the unchanged ``claimed_at`` (plus
        ``attribute_not_exists(seeded_at)``, so a seed that lands mid-steal
        can't be clobbered) -- so two simultaneous stealers can't both win.

        ``holder`` identifies THIS caller and is written onto the sentinel
        on both the fresh claim and a successful steal, exactly like the
        org-create claim's ``holder``. This closes a race the stale-steal
        takeover would otherwise reopen: without it, an original holder (A)
        that stalls past the stale window, gets stolen from by B, and only
        THEN fails and calls ``release_onboarding_claim`` would delete B's
        live claim out from under it -- letting a third request (C) win a
        fresh claim and seed a SECOND goal/project pair while B is still
        working. Generate a fresh ``holder`` per request (a retry by the
        same user must be distinguishable from their own prior attempt);
        pass the SAME value to the matching ``release_onboarding_claim``.

        Callers MUST ``release_onboarding_claim`` on failure so a retry (or
        a steal) can re-enter without waiting out the stale window.
        """
        key = _onboarding_claim_key(company_id)
        now = datetime.now(timezone.utc)
        item = {
            "user_id": key,
            "kind": "onboarding_claim",
            "company_id": company_id,
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
            return OnboardingClaimResult.WON
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

        # 2. A sentinel exists — strongly-consistent read, then decide.
        existing = await self.get_onboarding(company_id)
        if existing is None:
            # Released (deleted) between our put and get — try one fresh claim.
            try:
                await run_in_thread(
                    table.put_item,
                    Item=item,
                    ConditionExpression="attribute_not_exists(user_id)",
                )
                return OnboardingClaimResult.WON
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                # Someone else claimed it in the gap between our read and
                # write. We don't know their state without re-reading, and
                # a false SEEDED would wrongly tell our caller "done" — so
                # report the conservative option: retry. If it really is
                # SEEDED, the retry's own read will see that.
                return OnboardingClaimResult.IN_FLIGHT

        if existing.get("seeded_at"):
            # Genuinely done — never steal a completed seed.
            return OnboardingClaimResult.SEEDED

        prev_claimed_at = existing.get("claimed_at", "")
        try:
            age = (now - datetime.fromisoformat(prev_claimed_at)).total_seconds()
        except (ValueError, TypeError):
            age = float("inf")  # unparseable timestamp → treat as stale
        if age < stale_after_seconds:
            # A claim is legitimately in flight.
            return OnboardingClaimResult.IN_FLIGHT

        # Steal — overwrite (becoming the new holder) only if claimed_at is
        # unchanged since our read AND no seed has landed since, so two
        # concurrent stealers can't both win and a just-completed seed can't
        # be clobbered.
        try:
            await run_in_thread(
                table.put_item,
                Item=item,
                ConditionExpression="claimed_at = :prev AND attribute_not_exists(seeded_at)",
                ExpressionAttributeValues={":prev": prev_claimed_at},
            )
            return OnboardingClaimResult.WON
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            # Another stealer won the race, or a seed landed underneath us.
            # Same reasoning as above: report the conservative IN_FLIGHT
            # rather than guessing which.
            return OnboardingClaimResult.IN_FLIGHT

    async def release_onboarding_claim(self, company_id: str, *, holder: str) -> None:
        """Drop the claim so a failed seed can be retried -- iff we still
        hold it.

        Guarded on ``holder`` so we never delete a claim another caller has
        since stolen (mirrors ``release_org_create_claim``). Without this
        guard, an original holder that stalled past the stale window, got
        stolen from, and only then failed would delete the new holder's
        live claim out from under it -- reopening the duplicate-pair race
        the claim mechanism exists to prevent. A lost guard is a no-op, not
        an error: the claim is either already released, already re-claimed
        by someone else, or already completed -- none of which this caller
        should touch.
        """
        table = self._table()
        try:
            await run_in_thread(
                table.delete_item,
                Key={"user_id": _onboarding_claim_key(company_id)},
                ConditionExpression="holder = :h",
                ExpressionAttributeValues={":h": holder},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            pass

    async def record_onboarding_ids(self, company_id: str, *, goal_id: str, project_id: str, holder: str) -> None:
        """Record the seeded rows on the claim. Written LAST, so a claim
        carrying ids always means both Paperclip rows exist and are linked.

        Guarded with ``attribute_exists(user_id) AND holder = :h`` — TWO
        conditions, both load-bearing:

        * ``attribute_exists(user_id)`` — this can only ever update a row
          that ``claim_onboarding``'s PK-conditional ``put_item`` already
          created. Without it, ``update_item`` on a non-existent key
          silently CREATES it from scratch — a caller bug (e.g. calling
          this without ever claiming) would fabricate the sentinel out of
          thin air instead of failing loudly, permanently locking the
          company out of onboarding with no exception and no signal.
        * ``holder = :h`` — existence alone is not enough once
          ``claim_onboarding`` can steal: if THIS caller (A) stalls past
          the stale window, gets stolen from by B, then recovers and
          finishes seeding, the row still exists (it's B's now) -- so an
          existence-only guard would let A's ``update_item`` land and
          stamp A's ids over B's in-flight claim. Both seeded pairs would
          then exist with only one linked from the sentinel, orphaning the
          other on the user's first screen. The holder guard makes that
          write fail instead, routing A into its own cleanup (delete its
          own goal/project, then a holder-guarded release that no-ops
          against B's claim) while B proceeds untouched.

        Raises ``ConditionalCheckFailedException`` if no claim was ever
        taken under this key, OR if a different holder now owns it (having
        stolen it after this caller stalled). Callers release the claim on
        any failure here, but do NOT translate it into a 409 — the
        exception (e.g. botocore's ``ClientError``) propagates through a
        generic handler with no registered mapping for it and surfaces to
        the client as a raw 500.
        """
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": _onboarding_claim_key(company_id)},
            UpdateExpression="SET onboarding_goal_id = :g, onboarding_project_id = :p, seeded_at = :t",
            ExpressionAttributeValues={
                ":g": goal_id,
                ":p": project_id,
                ":t": datetime.now(timezone.utc).isoformat(),
                ":h": holder,
            },
            ConditionExpression="attribute_exists(user_id) AND holder = :h",
        )

    async def get_onboarding(self, company_id: str) -> Optional[dict]:
        """Read the sentinel. Strongly consistent — callers branch on it."""
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": _onboarding_claim_key(company_id)},
            ConsistentRead=True,
        )
        return resp.get("Item")

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
