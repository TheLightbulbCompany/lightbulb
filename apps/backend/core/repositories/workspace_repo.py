"""The workspace store: one META row per workspace (Clerk org) on the ``tasks`` table
(``WS#{org}/META``, camelCase attributes) plus its pending-brief sentinel (``WS#{org}/PENDING_BRIEF``).

Methods write snake_case expressions; ``_ws_call`` rewrites them to camelCase, so ``Workspace`` reads
like a plain row.

The legacy ``paperclip-companies`` table (a data name; retiring it is a later data task) is still read
for two things: the ``company_id`` of a workspace provisioned before the native store, and the per-user
identity rows the wipes and ``/teams/members`` read. ``delete`` removes both.
"""

from __future__ import annotations

import logging
import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from boto3.dynamodb.conditions import Key

from core import tasks_schema as ts
from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)


class FunnelClaimResult(str, Enum):
    """What ``claim_funnel_submission`` decided, for the caller to act on.

    The funnel claim is a generation-fenced attribute transition on the
    workspace row (``funnel_state: not_started -> submitted``): DynamoDB's own
    conditional write serializes concurrent submitters, and a losing caller is
    deterministic — it splits into exactly the two cases below by re-reading
    the row.
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
    underneath it); it should re-resolve the workspace row and retry, not treat
    this as a duplicate submit."""


WorkspaceStatus = Literal["seeding", "provisioning", "active", "failed", "disabled"]
FunnelState = Literal["not_started", "submitted", "complete"]


# Reserved user_id namespace for per-OWNER "the user answered the funnel, but
# their workspace wasn't ready to receive it yet" items. A synthetic PK:
# reached only by direct get/put and carrying none of the GSI key attributes
# (so it never appears in the by-org-id or by-status-purge-at indexes). The drain runs through
# core/services/onboarding_brief.reconcile_pending_brief.
_PENDING_BRIEF_PREFIX = "__pending_brief__:"


def _pending_brief_key(owner_id: str) -> str:
    return f"{_PENDING_BRIEF_PREFIX}{owner_id}"


# --- snake_case <-> tasks-table camelCase ------------------------------------
# Methods below write snake_case attribute names; ``_ws_call`` rewrites them for the ``tasks`` table.

_META_ONLY = frozenset({"sk", "metaKind", "workBackend"})


def _camel(name: str) -> str:
    if name == "user_id":
        return "pk"
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def _meta_item(item: dict, owner_id: str, sk: str) -> dict:
    """A snake_case workspace row (``sk=META``) or pending-brief sentinel (``sk=PENDING_BRIEF``) as its
    tasks-table item."""
    out = {_camel(k): v for k, v in item.items() if k != "user_id"}
    out.update(pk=ts.ws_pk(owner_id), sk=sk)
    if sk == ts.META_SK:
        out["metaKind"] = "workspace"
    return out


def from_meta_item(item: dict) -> dict:
    """A tasks-table META / PENDING_BRIEF row (or a partial ``Attributes`` map) back in the old
    table's snake_case shape, ``user_id`` included when the row carries its keys."""
    out = {_snake(k): v for k, v in item.items() if k not in _META_ONLY and k != "pk"}
    if "pk" in item:
        owner = item["pk"].removeprefix("WS#")
        out["user_id"] = _pending_brief_key(owner) if item.get("sk") == ts.PENDING_BRIEF_SK else owner
    return out


# A bare attribute name in an update/condition/projection expression: not a ``:value`` or
# ``#name`` placeholder, not a map key after ``.``, not a function name.
_EXPR_NAME = re.compile(r"(?<![:#\w.])[a-z][a-z0-9_]*\b(?!\s*\()")


def _tasks_expressions(kwargs: dict) -> dict:
    """Rewrite each bare snake_case attribute in the expressions to a ``#camelCase`` name."""
    names = dict(kwargs.get("ExpressionAttributeNames") or {})

    def sub(match: re.Match) -> str:
        camel = _camel(match.group(0))
        names[f"#{camel}"] = camel
        return f"#{camel}"

    out = dict(kwargs)
    for key in ("UpdateExpression", "ConditionExpression", "ProjectionExpression"):
        if key in out:
            out[key] = _EXPR_NAME.sub(sub, out[key])
    if names:
        out["ExpressionAttributeNames"] = names
    return out


# The single "genuinely not started" onboarding-state shape -- SOLE owner
# of this key set. ``get_onboarding_state`` overlays the one dynamic field
# (`pending_brief`, computed per-call) on top of this; every other caller
# needing an all-defaults onboarding dict (notably
# ``core.services.readiness_state``'s degrade-to-defaults except branch)
# must import THIS rather than hand-copy the keys -- a hand-copy already
# drifted out of sync once (commit 6b42f427e / #1820).
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
    "user_id": None,
}


def _iso_or_none(item: dict, key: str) -> Optional[datetime]:
    return datetime.fromisoformat(item[key]) if item.get(key) else None


@dataclass
class Workspace:
    """The ONE row of a workspace (Clerk org), keyed by the org id itself."""

    org_id: str
    """Clerk org id — also this row's key."""

    company_id: str
    """Legacy company id, read from the old table for workspaces provisioned before the native store; "" otherwise.
    Never written."""

    status: WorkspaceStatus
    created_at: datetime
    updated_at: datetime

    provisioner_user_id: str = ""
    """The Clerk user whose ``provision_org`` created the company."""

    member_user_ids: set[str] = dataclasses.field(default_factory=set)
    """Clerk users who have JOINED this workspace
    (``provision_org`` for the provisioner, ``provision_member`` for everyone
    else). ``archive_member`` removes one. This is the Teams authorization
    fence and ``provision_member``'s idempotency record."""

    last_error: Optional[str] = None
    scheduled_purge_at: Optional[datetime] = None
    seeded_agent_ids: Optional[list[str]] = None
    """Legacy agent IDs from the Friday/Atlas founder seed.

    Retained only for those pre-starter-roster workspaces; read for the Teams
    ``seeded`` flag. Matching on persisted IDs (rather than role/name) means a
    user-created agent that happens to use role ``ceo`` or the name ``Atlas``
    is never mistaken for a legacy-seeded one."""

    starter_agent_ids: Optional[dict[str, str]] = None
    """Starter role key -> agent id for fresh workspaces.

    The keys are ``ceo``, ``engineering``, ``finance``, ``growth``,
    ``analytics``, and ``customer_operations``. Unlike ``seeded_agent_ids``,
    it records provenance and reconciliation identity; it does not make an
    agent undeletable.
    """

    # ------------------------------------------------------------------
    # Onboarding (co-located per the 2026-07-24 architecture decision —
    # NO dedicated table; these fields ride on the workspace row).
    # ------------------------------------------------------------------

    onboarding_generation: Optional[str] = None
    """ULID fence for onboarding facts (the company-incarnation fence, G2).
    Minted exactly once, in ``provision_org``'s fresh-create branch, AFTER a
    genuinely-new workspace is confirmed — never on reconcile-adopt
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
    its onboarding state. Fulfilment turns it into ``BRIEF.md``."""

    onboarding_submitted_by: Optional[str] = None
    """The Clerk member who sent the captured first message, written on the
    same claim as ``funnel_submitted_at``."""

    first_contact_state: Optional[str] = None
    """``not_started`` -> ``sending`` -> ``sent`` | ``failed``.

    THE once-only guard for the agent-initiated first email. Policy is read
    from HERE, never from the ``purpose`` label a container supplies.
    ``failed`` is deliberately RE-CLAIMABLE; ``sending`` and ``sent`` are not
    — reclaiming either risks a double-send."""

    first_contact_attempts: Optional[int] = None
    """Bounds the nudge loop, and is the ONLY thing that does."""

    first_contact_reason: Optional[str] = None

    name: Optional[str] = None
    description: Optional[str] = None
    """The workspace's display name and description (``/teams/settings``). Written only by
    ``set_profile``; ``put_workspace`` never sets or removes them."""


@dataclass
class Identity:
    """One human's legacy identity row, keyed by their Clerk user id."""

    user_id: str
    paperclip_user_id: str = ""
    paperclip_email: str = ""


_WORKSPACE_ISO_FIELDS = ("scheduled_purge_at", "funnel_submitted_at", "funnel_completed_at")
_WORKSPACE_PLAIN_FIELDS = (
    "last_error",
    "onboarding_generation",
    "funnel_state",
    "onboarding_goal_id",
    "onboarding_project_id",
    "onboarding_brief_text",
    "onboarding_submitted_by",
    "first_contact_state",
    "first_contact_attempts",
    "first_contact_reason",
)
_WORKSPACE_OPTIONAL_FIELDS = (
    "provisioner_user_id",
    "member_user_ids",
    "seeded_agent_ids",
    "starter_agent_ids",
    *_WORKSPACE_PLAIN_FIELDS,
    *_WORKSPACE_ISO_FIELDS,
)


class WorkspaceRepo:
    """Async repository for the workspace META store (and the legacy table above).

    The table name is injected so the same repo class is usable for the
    real per-environment table and for moto-backed unit tests. Internally we
    resolve the boto3 Table via ``core.dynamodb.get_table`` so prefix-patching
    in tests Just Works.
    """

    def __init__(self, table_name: str = "paperclip-companies", region: str | None = None):  # noqa: ARG002 - region kept for back-compat
        self._table_name = table_name

    def _table(self):
        return get_table(self._table_name)

    def _ws_table(self):
        return get_table("tasks")

    def _ws_key(self, org_id: str) -> dict:
        return {"pk": ts.ws_pk(org_id), "sk": ts.META_SK}

    def _pending_key(self, owner_id: str) -> dict:
        return {"pk": ts.ws_pk(owner_id), "sk": ts.PENDING_BRIEF_SK}

    async def _ws_call(self, method: str, key: dict | None = None, **kwargs) -> dict:
        """One call on the META store. Callers write snake_case expressions; they are rewritten to
        camelCase and the returned ``Item``/``Attributes`` come back snake_case."""
        if key is not None:
            kwargs["Key"] = key
        resp = await run_in_thread(getattr(self._ws_table(), method), **_tasks_expressions(kwargs))
        for k in ("Item", "Attributes"):
            if resp.get(k):
                resp[k] = from_meta_item(resp[k])
        return resp

    # ------------------------------------------------------------------
    # Workspace rows
    # ------------------------------------------------------------------

    async def put_workspace(
        self,
        ws: Workspace,
        *,
        condition: str | None = None,
        condition_values: dict | None = None,
    ) -> None:
        """Write every ``Workspace`` field of a workspace row.

        One ``UpdateItem`` that SETs each field ``ws`` carries and REMOVEs each
        one it does not, so it replaces the dataclass's fields exactly like a
        whole-item put did. ``company_id``, ``name`` and ``description`` are never written here.

        ⚠️ WRITER-SAFETY: any dataclass field NOT set on ``ws`` is REMOVED,
        which silently WIPES a previously written value
        (``onboarding_generation``, ``funnel_state``, members, ...). Every caller MUST carry forward the row's existing fields unless
        it is deliberately establishing them fresh (the G2 mint site in
        ``workspace_provisioning.provision_org``). Prefer the conditional
        ``UpdateItem`` methods below for any single-field write.

        ``condition``/``condition_values`` are used ONLY by the G2 mint site
        (``attribute_not_exists(onboarding_generation) OR onboarding_generation
        = :g``) so a concurrent peer's already-persisted generation wins. On
        mismatch boto3 raises ``ClientError`` (uncaught here).
        """
        item: dict = {
            "org_id": ws.org_id,
            "status": ws.status,
            "created_at": ws.created_at.isoformat(),
            "updated_at": ws.updated_at.isoformat(),
        }
        if ws.provisioner_user_id:
            item["provisioner_user_id"] = ws.provisioner_user_id
        if ws.member_user_ids:
            item["member_user_ids"] = set(ws.member_user_ids)
        if ws.seeded_agent_ids:
            item["seeded_agent_ids"] = ws.seeded_agent_ids
        if ws.starter_agent_ids:
            item["starter_agent_ids"] = ws.starter_agent_ids
        for name in _WORKSPACE_PLAIN_FIELDS:
            value = getattr(ws, name)
            if value is not None:
                item[name] = value
        for name in _WORKSPACE_ISO_FIELDS:
            value = getattr(ws, name)
            if value is not None:
                item[name] = value.isoformat()
        names = {f"#f{i}": _camel(k) for i, k in enumerate(item)}
        values = {f":f{i}": v for i, v in enumerate(item.values())}
        expr = "SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(item))) + ", #mk = :mk"
        names["#mk"] = "metaKind"
        values[":mk"] = "workspace"
        absent = [k for k in _WORKSPACE_OPTIONAL_FIELDS if k not in item]
        if absent:
            names.update({f"#r{i}": _camel(k) for i, k in enumerate(absent)})
            expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(absent)))
        kwargs: dict = {
            "UpdateExpression": expr,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": {**values, **(condition_values or {})},
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        await self._ws_call("update_item", self._ws_key(ws.org_id), **kwargs)
        publish_status_changed(ws.org_id)

    async def get_workspace(self, org_id: str, *, consistent_read: bool = False) -> Optional[Workspace]:
        resp = await self._ws_call("get_item", self._ws_key(org_id), ConsistentRead=consistent_read)
        item = resp.get("Item")
        # A missing ``status`` means the key names something other than a
        # workspace row (a pre-migration human row, a sentinel): not a workspace.
        if not item or "status" not in item:
            return None
        # A workspace provisioned before the native store keeps its legacy ``company_id`` on the old row.
        old = await run_in_thread(self._table().get_item, Key={"user_id": org_id}, ConsistentRead=consistent_read)
        if (old.get("Item") or {}).get("company_id"):
            item["company_id"] = old["Item"]["company_id"]
        return self._item_to_workspace(item)

    @staticmethod
    def _item_to_workspace(item: dict) -> Workspace:
        return Workspace(
            org_id=item["user_id"],
            company_id=item.get("company_id", ""),
            status=item["status"],
            created_at=datetime.fromisoformat(item["created_at"]),
            updated_at=datetime.fromisoformat(item["updated_at"]),
            provisioner_user_id=item.get("provisioner_user_id", ""),
            member_user_ids=set(item.get("member_user_ids") or ()),
            last_error=item.get("last_error"),
            scheduled_purge_at=_iso_or_none(item, "scheduled_purge_at"),
            seeded_agent_ids=(list(item["seeded_agent_ids"]) if item.get("seeded_agent_ids") else None),
            starter_agent_ids=(dict(item["starter_agent_ids"]) if item.get("starter_agent_ids") else None),
            onboarding_generation=item.get("onboarding_generation"),
            funnel_state=item.get("funnel_state"),
            funnel_submitted_at=_iso_or_none(item, "funnel_submitted_at"),
            funnel_completed_at=_iso_or_none(item, "funnel_completed_at"),
            onboarding_goal_id=item.get("onboarding_goal_id"),
            onboarding_project_id=item.get("onboarding_project_id"),
            onboarding_brief_text=item.get("onboarding_brief_text"),
            onboarding_submitted_by=item.get("onboarding_submitted_by"),
            first_contact_state=item.get("first_contact_state"),
            first_contact_attempts=(
                int(item["first_contact_attempts"]) if item.get("first_contact_attempts") is not None else None
            ),
            first_contact_reason=item.get("first_contact_reason"),
            name=item.get("name"),
            description=item.get("description"),
        )

    async def resolve_owner_row(
        self, owner_id: str, *, consistent_read: bool = False, include_inactive: bool = False
    ) -> Optional[Workspace]:
        """The workspace row for ``owner_id`` (a Clerk org id): one GetItem.

        Returns None when there is none, or — unless ``include_inactive`` —
        when it is not ``active`` (a disabled/provisioning/failed company is
        not usable). STATUS readers (``get_onboarding_state``) pass
        ``include_inactive=True`` so a failed seed surfaces as
        ``teams_state="failed"``.
        """
        ws = await self.get_workspace(owner_id, consistent_read=consistent_read)
        if ws is None or (not include_inactive and ws.status != "active"):
            return None
        return ws

    async def add_member(self, org_id: str, user_id: str) -> None:
        """Record that ``user_id`` joined this workspace.
        Conditional on the workspace row existing (never resurrects a partial)."""
        await self._ws_call(
            "update_item",
            self._ws_key(org_id),
            UpdateExpression="ADD member_user_ids :u SET updated_at = :t",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeValues={":u": {user_id}, ":t": utc_now_iso()},
        )
        publish_status_changed(org_id)

    async def set_profile(self, org_id: str, *, name: str | None, description: str | None) -> None:
        """Set the workspace ``name`` and/or ``description`` (None leaves one as it is).
        Conditional on the row existing (never resurrects a partial)."""
        fields = {k: v for k, v in (("name", name), ("description", description)) if v is not None}
        if not fields:
            return
        await self._ws_call(
            "update_item",
            self._ws_key(org_id),
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields) + ", updated_at = :t",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={f"#{k}": k for k in fields},  # `name` is a reserved word
            ExpressionAttributeValues={**{f":{k}": v for k, v in fields.items()}, ":t": utc_now_iso()},
        )

    async def remove_member(self, org_id: str, user_id: str) -> None:
        """Drop ``user_id`` from the workspace's members. A no-op when the
        workspace row is gone (never resurrects a partial)."""
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
                UpdateExpression="DELETE member_user_ids :u SET updated_at = :t",
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues={":u": {user_id}, ":t": utc_now_iso()},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return
        publish_status_changed(org_id)

    async def _query_pages(self, table=None, **kwargs):
        """Yield each raw Query response page, following ``LastEvaluatedKey``."""
        while True:
            resp = await run_in_thread((table or self._table()).query, **kwargs)
            yield resp
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return
            kwargs = {**kwargs, "ExclusiveStartKey": last_key}

    async def update_status(
        self,
        key: str,
        *,
        status: WorkspaceStatus,
        last_error: Optional[str] = None,
        scheduled_purge_at: Optional[datetime] = None,
        not_if_status: Optional[WorkspaceStatus] = None,
    ) -> bool:
        """Update a workspace row's status. Returns True iff the write landed.

        ``not_if_status`` guards the write so it is SKIPPED (returns False)
        when the row is currently in that status — a losing concurrent
        provision attempt must not downgrade an already-``active`` workspace
        to ``failed``. It also requires the row to exist.
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

        kwargs: dict = {
            "UpdateExpression": update_expr,
            "ExpressionAttributeNames": expr_names,
            "ExpressionAttributeValues": expr_values,
        }
        if not_if_status is not None:
            kwargs["ConditionExpression"] = "attribute_exists(user_id) AND #s <> :guard"
            expr_values[":guard"] = not_if_status

        table = self._ws_table()
        try:
            await self._ws_call("update_item", self._ws_key(key), **kwargs)
            publish_status_changed(key)
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def put_failed_marker_if_absent(self, *, org_id: str, reason: str) -> bool:
        """Durably record "this workspace's seed failed" when NO workspace row
        exists yet, via a CONDITIONAL put-if-absent. Returns True iff it landed.

        The marker carries no ``company_id``: its only job is durable
        visibility (``/teams`` → 503, ``teams_status="failed"``) instead of the
        202 spinner a MISSING row produces. A put-if-absent can only ever
        create, never overwrite a real row a concurrent provision wrote.
        """
        now = utc_now_iso()
        item = {
            "user_id": org_id,
            "org_id": org_id,
            "status": "failed",
            "last_error": reason,
            "created_at": now,
            "updated_at": now,
        }
        table = self._ws_table()
        try:
            await self._ws_call(
                "put_item",
                Item=_meta_item(item, org_id, ts.META_SK),
                ConditionExpression="attribute_not_exists(user_id)",
            )
            publish_status_changed(org_id)
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def delete(self, key: str) -> None:
        """Delete ``key``'s old-table row (identity or legacy workspace) and its META row."""
        await run_in_thread(self._table().delete_item, Key={"user_id": key})
        await run_in_thread(self._ws_table().delete_item, Key=self._ws_key(key))

    # ------------------------------------------------------------------
    # Identity rows
    # ------------------------------------------------------------------

    async def get_identity(self, user_id: str, *, consistent_read: bool = False) -> Optional[Identity]:
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": user_id},
            ConsistentRead=consistent_read,
        )
        item = resp.get("Item")
        if not item:
            return None
        return Identity(
            user_id=item["user_id"],
            paperclip_user_id=item.get("paperclip_user_id", ""),
            paperclip_email=item.get("paperclip_email", ""),
        )

    async def get_onboarding_state(self, owner_id: str) -> dict:
        """RECONCILE-WITH-ONBOARD: read-only accessor for the
        COMPOSER/ONBOARD interface contract.

        Returns the onboarding keys plus ``teams_state`` (the workspace row's
        raw ``status``), ``company_id`` and ``user_id`` (the workspace's
        ``provisioner_user_id``). ``owner_id`` is the workspace (Clerk org) id:
        one GetItem, shared by ``core.services.readiness_state.compute_status``
        and the pending-brief reconciler.

        A row that doesn't exist yet, or that predates ONBOARD's writers,
        returns every onboarding field at its "genuinely not started"
        default -- never raises (read failures are the caller's
        responsibility to guard, matching every other method here).
        """
        # include_inactive=True: a status reader must still resolve a
        # FAILED seed row so teams_state="failed" surfaces.
        company = await self.resolve_owner_row(owner_id, include_inactive=True)
        if company is None:
            # ONBOARDING_STATE_DEFAULTS covers every static field; only
            # `pending_brief` is dynamic (per-owner DDB lookup) and gets
            # overlaid here.
            return {
                **ONBOARDING_STATE_DEFAULTS,
                "pending_brief": await self._has_pending_brief(owner_id),
            }

        # getattr(..., default): some test doubles stand in a bare
        # SimpleNamespace(status=...) for the workspace row.
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
            "user_id": getattr(company, "provisioner_user_id", None) or None,
        }

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
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(company.org_id),
                UpdateExpression="REMOVE starter_agent_ids.#starter_key",
                ConditionExpression="starter_agent_ids.#starter_key = :agent_id",
                ExpressionAttributeNames={"#starter_key": starter_key},
                ExpressionAttributeValues={":agent_id": agent_id},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    async def claim_first_contact(self, org_id: str, *, onboarding_generation: str) -> bool:
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
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
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
            logger.warning("claim_first_contact failed for %s: %s", org_id, e)
            return False

    async def settle_first_contact(
        self,
        org_id: str,
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
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
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
            logger.warning("settle_first_contact failed for %s: %s", org_id, e)

    async def claim_funnel_submission(
        self,
        org_id: str,
        *,
        onboarding_generation: str,
        submitted_by: str,
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
        message. Any room member can supply that first message.

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
        table = self._ws_table()
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
                await self._ws_call(
                    "update_item",
                    self._ws_key(org_id),
                    UpdateExpression=(
                        "SET funnel_state = :submitted, funnel_submitted_at = :t, "
                        "onboarding_submitted_by = :sub, updated_at = :u" + brief_set
                    ),
                    ConditionExpression="onboarding_generation = :gen AND funnel_state = :not_started",
                    ExpressionAttributeValues=values,
                )
                publish_status_changed(org_id)
                return FunnelClaimResult.WON
            except table.meta.client.exceptions.ConditionalCheckFailedException:
                pass

            current = await self.get_workspace(org_id, consistent_read=True)
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

    async def revert_funnel_submission(self, org_id: str, *, onboarding_generation: str) -> None:
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
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
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
        publish_status_changed(org_id)

    async def complete_funnel_submission(self, org_id: str, *, onboarding_generation: str) -> bool:
        """Complete the claimed generation once its BRIEF.md is staged."""
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
                UpdateExpression="SET funnel_state = :complete, funnel_completed_at = :t, updated_at = :t",
                ConditionExpression="onboarding_generation = :gen AND funnel_state = :submitted",
                ExpressionAttributeValues={
                    ":complete": "complete",
                    ":submitted": "submitted",
                    ":gen": onboarding_generation,
                    ":t": utc_now_iso(),
                },
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False
        publish_status_changed(org_id)
        return True

    # --- Pending brief (funnel intake before the workspace can receive it) ---
    #
    # The first canonical-room human message is intake: it records what the
    # user said, then normal dispatch continues. Everything that needs
    # the workspace to be ready (the claim, goal/project seed, and BRIEF.md staging)
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

        table = self._ws_table()
        item = _meta_item(item, owner_id, ts.PENDING_BRIEF_SK)
        if overwrite:
            await self._ws_call("put_item", Item=item)
            return True
        try:
            await self._ws_call(
                "put_item",
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
            logger.info("workspace_repo: pending-brief read failed for owner %s", owner_id, exc_info=True)
            return False
        return item is not None and item.get("brief_state") != "failed"

    async def get_pending_brief(self, owner_id: str) -> Optional[dict]:
        """The pending funnel answer for this owner, or None.

        Strongly consistent: the drain reads this and then conditionally
        writes the owner row off what it finds, and a status poll that missed
        a just-written item would re-show the funnel to someone who already
        answered it.
        """
        resp = await self._ws_call("get_item", self._pending_key(owner_id), ConsistentRead=True)
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
        table = self._ws_table()
        try:
            resp = await self._ws_call(
                "update_item",
                self._pending_key(owner_id),
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

    async def fail_pending_brief(self, owner_id: str, *, reason: str) -> None:
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
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._pending_key(owner_id),
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
        publish_status_changed(owner_id)

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
        resp = await self._ws_call("delete_item", self._pending_key(owner_id), ReturnValues="ALL_OLD")
        return bool(resp.get("Attributes"))

    async def list_active_workspace_members(self) -> dict[str, set[str]]:
        """``{org_id: member_user_ids}`` for every ACTIVE workspace (the ``ws-meta`` index)."""
        from boto3.dynamodb.conditions import Attr

        out: dict[str, set[str]] = {}
        async for page in self._query_pages(
            self._ws_table(),
            IndexName="ws-meta",
            KeyConditionExpression=Key("metaKind").eq("workspace"),
            FilterExpression=Attr("status").eq("active"),
            ProjectionExpression="pk, memberUserIds",
        ):
            for item in page.get("Items", []):
                out[item["pk"].removeprefix("WS#")] = set(item.get("memberUserIds") or ())
        return out

    async def scan_purge_due(self, now: datetime) -> list[Workspace]:
        """Disabled workspaces whose ``scheduled_purge_at <= now`` (the ``ws-meta`` index; each hit is re-read)."""
        from boto3.dynamodb.conditions import Attr

        out: list[Workspace] = []
        pages = self._query_pages(
            self._ws_table(),
            IndexName="ws-meta",
            KeyConditionExpression=Key("metaKind").eq("workspace"),
            FilterExpression=Attr("status").eq("disabled") & Attr("scheduledPurgeAt").lte(now.isoformat()),
            ProjectionExpression="pk",
        )
        async for page in pages:
            for item in page.get("Items", []):
                org_id = item["pk"].removeprefix("WS#")
                ws = await self.get_workspace(org_id)
                if ws is not None:
                    out.append(ws)
                else:
                    logger.warning(
                        "workspace_repo.scan_purge_due: GSI returned %s but base item missing",
                        org_id,
                    )
        return out


async def customer_ops_agent_id(owner_id: str) -> str | None:
    """The workspace's Customer Operations starter agent (the front desk), or None when the roster has none."""
    row = await WorkspaceRepo().resolve_owner_row(owner_id)
    return (getattr(row, "starter_agent_ids", None) or {}).get("customer_operations")
