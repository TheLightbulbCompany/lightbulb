"""DynamoDB repository for the paperclip-companies table.

PK attribute: ``user_id`` (S). Despite the attribute name, the table holds
two kinds of row (plus the pending-brief sentinels below):

* **Workspace row** — keyed by the Clerk ORG id itself (``org_…``). ONE per
  workspace: one workspace = one Paperclip company. Carries every COMPANY
  fact — ``company_id``, ``status``, the starter/seeded roster ids, the
  onboarding funnel, the purge timer, who provisioned it
  (``provisioner_user_id``) and which humans have joined the company
  (``member_user_ids``). Org ids can never collide with ``user_…`` keys.
* **Identity row** — keyed by a Clerk USER id. ONLY that human's Better Auth
  identity (``paperclip_user_id`` / ``paperclip_email`` /
  ``paperclip_password_encrypted``). A human has one Better Auth account no
  matter how many workspaces they belong to, so the row names no company and
  no org. Deleting it can never delete a company — the 2026-09-15 wipe of the
  founder's shared company came from the old shape, where every member's row
  also carried the company.

An identity row carries ``status`` only while its sign-up is unsettled: the
``seeding`` claim that fences a concurrent duplicate sign-up, or ``failed``
when that sign-up failed before an identity was confirmed. ``put_identity``
writes the settled row without it.

Async repository following the peer repos in ``core/repositories/``: every
DynamoDB call is wrapped in ``run_in_thread``.

Workspace META store (spec 03 §4.4, wave1-R Task 6): ``_meta_on_tasks()``
moves workspace rows and pending-brief sentinels to the ``tasks`` table
(``WS#{org}/META`` and ``WS#{org}/PENDING_BRIEF``, camelCase attributes,
translated here so ``PaperclipWorkspace`` and every public method are
unchanged). Identity rows, ``org_company_ids`` and ``find_workspace_by_company``
stay on this table, and so do the Paperclip-only workspace attributes
(``company_id``, ``agent_keys``, ``agent_cost_remainder``, the
``automation_pause*`` fence): they are never copied to META and are read
back from the old row.
"""

from __future__ import annotations

import logging
import dataclasses
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core import tasks_schema as ts
from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.encryption import decrypt, encrypt
from core.services.status_events import publish_status_changed
from core.services.super_agent_flags import env_enabled

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


PaperclipCompanyStatus = Literal["seeding", "provisioning", "active", "failed", "disabled"]
FunnelState = Literal["not_started", "submitted", "complete"]


# Reserved user_id namespace for per-OWNER "the user answered the funnel, but
# their workspace wasn't ready to receive it yet" items. A synthetic PK:
# reached only by direct get/put and carrying none of the GSI key attributes
# (so it never appears in the by-org-id or by-status-purge-at indexes). The drain runs through
# core/services/onboarding_brief.reconcile_pending_brief.
_PENDING_BRIEF_PREFIX = "__pending_brief__:"


def _pending_brief_key(owner_id: str) -> str:
    return f"{_PENDING_BRIEF_PREFIX}{owner_id}"


# --- tasks-table META translation (spec 03 §6.2) ---------------------------

_OLD_ROW_ONLY = frozenset({"company_id", "agent_keys", "agent_cost_remainder"})
_META_ONLY = frozenset({"sk", "metaKind", "workBackend"})


def old_row_only(name: str) -> bool:
    """Paperclip-only workspace attributes: never copied to META, kept on the old row."""
    return name in _OLD_ROW_ONLY or name.startswith("automation_pause")


def _camel(name: str) -> str:
    if name == "user_id":
        return "pk"
    head, *rest = name.split("_")
    return head + "".join(part.title() for part in rest)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)([A-Z])", r"_\1", name).lower()


def to_meta_item(item: dict, owner_id: str, sk: str) -> dict:
    """An old-table workspace row (``sk=META``) or pending-brief sentinel (``sk=PENDING_BRIEF``)
    as its tasks-table row. Drops the Paperclip-only attributes."""
    out = {_camel(k): v for k, v in item.items() if k != "user_id" and not old_row_only(k)}
    out.update(pk=ts.ws_pk(owner_id), sk=sk)
    if sk == ts.META_SK:
        out.update(metaKind="workspace", workBackend=out.get("workBackend", "paperclip"))
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
class PaperclipWorkspace:
    """The ONE row of a workspace (Clerk org), keyed by the org id itself."""

    org_id: str
    """Clerk org id — also this row's key."""

    company_id: str
    """The workspace's Paperclip company. "" on a pre-company ``failed`` marker."""

    status: PaperclipCompanyStatus
    created_at: datetime
    updated_at: datetime

    provisioner_user_id: str = ""
    """The Clerk user whose ``provision_org`` created the company."""

    member_user_ids: set[str] = dataclasses.field(default_factory=set)
    """Clerk users who have JOINED this workspace's Paperclip company
    (``provision_org`` for the provisioner, ``provision_member`` for everyone
    else). ``archive_member`` removes one. This is the Teams authorization
    fence and ``provision_member``'s idempotency record."""

    last_error: Optional[str] = None
    scheduled_purge_at: Optional[datetime] = None
    seeded_agent_ids: Optional[list[str]] = None
    """Legacy Paperclip agent IDs from the Friday/Atlas founder seed.

    Retained only for those pre-starter-roster companies. It marks agents
    protected by that historical provisioning contract and is used by
    ``paperclip_openclaw_pairing.is_seeded_agent`` and the Teams BFF
    ``seeded`` flag. Matching on persisted IDs (rather than role/name) means a
    user-created agent that happens to use role ``ceo`` or the name ``Atlas``
    is never mistaken for a legacy-seeded one."""

    starter_agent_ids: Optional[dict[str, str]] = None
    """Starter role key -> Paperclip agent UUID for fresh workspaces.

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

    automation_paused_at: Optional[str] = None
    """Set by ``paperclip_agent_lifecycle.pause_owner_automation`` when the owner stopped
    being live; removed by ``resume_owner_automation``. The two sets below name exactly
    what WE paused, so a resume never re-enables a routine or heartbeat the owner turned
    off themselves (the starter routines are seeded paused)."""
    automation_paused_agent_ids: Optional[set[str]] = None
    automation_paused_routine_ids: Optional[set[str]] = None
    automation_pause_complete_at: Optional[str] = None
    automation_paused_routine_versions: Optional[dict[str, str]] = None
    automation_pause_token: Optional[str] = None
    """The running pause's fence: written with the record, removed by the resume. A pause
    re-checks it before every PATCH and stops once it no longer owns the row."""
    """routine id -> its ``updatedAt`` right after our pause PATCH; resume re-activates a
    routine only if it still matches (nobody touched it since)."""
    """Set only after a pause's Paperclip PATCHes ALL landed; every new pause removes it
    first. The expired-trial sweep re-runs the (idempotent) pause until this is set."""


@dataclass
class PaperclipIdentity:
    """One human's Better Auth identity, keyed by their Clerk user id."""

    user_id: str
    paperclip_user_id: str = ""
    """Better Auth user ID. "" until the sign-up is confirmed."""

    paperclip_password_encrypted: str = ""
    """Fernet-encrypted random password for the Better Auth account."""

    paperclip_email: str = ""
    """The address Better Auth actually holds for ``paperclip_user_id``.

    Better Auth's ``user.email`` is written once at sign-up and NEVER
    updated, so it is NOT interchangeable with the user's live Clerk primary
    email: signing in with a moved Clerk address 401s, falls through to
    sign-UP, and mints a DUPLICATE Better Auth account. Every sign-in door
    reads this first and falls back to the live Clerk address only when it
    is empty."""

    status: Optional[str] = None
    """Only on an unsettled sign-up: ``seeding`` (the claim) or ``failed``
    (sign-up failed before an identity was confirmed). None once settled."""

    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
    "automation_paused_at",
    "automation_paused_agent_ids",
    "automation_paused_routine_ids",
    "automation_pause_complete_at",
    "automation_paused_routine_versions",
    "automation_pause_token",
)
_WORKSPACE_OPTIONAL_FIELDS = (
    "company_id",
    "provisioner_user_id",
    "member_user_ids",
    "seeded_agent_ids",
    "starter_agent_ids",
    *_WORKSPACE_PLAIN_FIELDS,
    *_WORKSPACE_ISO_FIELDS,
)


class PaperclipRepo:
    """Async repository for the paperclip-companies DynamoDB table.

    The table name is injected so the same repo class is usable for the
    real per-environment table and for moto-backed unit tests. Internally we
    resolve the boto3 Table via ``core.dynamodb.get_table`` so prefix-patching
    in tests Just Works.
    """

    def __init__(self, table_name: str, region: str | None = None):  # noqa: ARG002 - region kept for back-compat
        self._table_name = table_name

    def _table(self):
        return get_table(self._table_name)

    def _meta_on_tasks(self) -> bool:
        """Whether workspace rows and pending briefs live on the ``tasks`` table (spec 03 §4.4).
        The env master: dev only, and only after ``copy_workspace_meta --all --verify`` is clean."""
        return env_enabled()

    def _ws_table(self):
        return get_table("tasks") if self._meta_on_tasks() else self._table()

    def _ws_key(self, org_id: str) -> dict:
        if self._meta_on_tasks():
            return {"pk": ts.ws_pk(org_id), "sk": ts.META_SK}
        return {"user_id": org_id}

    def _pending_key(self, owner_id: str) -> dict:
        if self._meta_on_tasks():
            return {"pk": ts.ws_pk(owner_id), "sk": ts.PENDING_BRIEF_SK}
        return {"user_id": _pending_brief_key(owner_id)}

    def _ws_item(self, item: dict, owner_id: str, sk: str) -> dict:
        return to_meta_item(item, owner_id, sk) if self._meta_on_tasks() else item

    async def _ws_call(self, method: str, key: dict | None = None, **kwargs) -> dict:
        """One call on the META store. Callers write snake_case expressions; on ``tasks`` they are
        rewritten to camelCase and the returned ``Item``/``Attributes`` come back snake_case."""
        if key is not None:
            kwargs["Key"] = key
        on_tasks = self._meta_on_tasks()
        if on_tasks:
            kwargs = _tasks_expressions(kwargs)
        resp = await run_in_thread(getattr(self._ws_table(), method), **kwargs)
        if on_tasks:
            for k in ("Item", "Attributes"):
                if resp.get(k):
                    resp[k] = from_meta_item(resp[k])
        return resp

    def _old_row_update(self, org_id: str, status: str, attrs: dict, absent: list[str]) -> dict:
        """On ``tasks``: the old-row half of ``put_workspace``, keeping the Paperclip-only
        attributes there. ``org_id`` keeps the by-org-id guard working; ``status`` (first write
        only) is what the agent-key writes condition on."""
        values = {f":o{i}": v for i, v in enumerate(attrs.values())}
        names = {f"#o{i}": k for i, k in enumerate(attrs)}
        sets = ["org_id = :org", "#s = if_not_exists(#s, :s)"] + [f"#o{i} = :o{i}" for i in range(len(attrs))]
        expr = "SET " + ", ".join(sets)
        if absent:
            names.update({f"#x{i}": k for i, k in enumerate(absent)})
            expr += " REMOVE " + ", ".join(f"#x{i}" for i in range(len(absent)))
        return {
            "TableName": self._table().name,
            "Key": {"user_id": org_id},
            "UpdateExpression": expr,
            "ExpressionAttributeNames": {"#s": "status", **names},
            "ExpressionAttributeValues": {":org": org_id, ":s": status, **values},
        }

    # ------------------------------------------------------------------
    # Workspace rows
    # ------------------------------------------------------------------

    async def put_workspace(
        self,
        ws: PaperclipWorkspace,
        *,
        condition: str | None = None,
        condition_values: dict | None = None,
    ) -> None:
        """Write every ``PaperclipWorkspace`` field of a workspace row.

        One ``UpdateItem`` that SETs each field ``ws`` carries and REMOVEs each
        one it does not, so it replaces the dataclass's fields exactly like a
        whole-item put did. Attributes the dataclass does not model (the
        per-agent ``agent_keys`` map, ``agent_cost_remainder``) are never
        written here: they change only through their own nested
        ``SET``/``REMOVE`` paths, so a stale snapshot cannot erase a concurrent
        write to them.

        ⚠️ WRITER-SAFETY: any dataclass field NOT set on ``ws`` is REMOVED,
        which silently WIPES a previously written value
        (``onboarding_generation``, ``funnel_state``, members, ...). Every caller MUST carry forward the row's existing fields unless
        it is deliberately establishing them fresh (the G2 mint site in
        ``paperclip_provisioning.provision_org``). Prefer the conditional
        ``UpdateItem`` methods below for any single-field write.

        ``condition``/``condition_values`` are used ONLY by the G2 mint site
        (``attribute_not_exists(onboarding_generation) OR onboarding_generation
        = :g``) so a concurrent peer's already-persisted generation wins. On
        mismatch boto3 raises ``ClientError`` (uncaught here).
        """
        item: dict = {
            "user_id": ws.org_id,
            "org_id": ws.org_id,
            "status": ws.status,
            "created_at": ws.created_at.isoformat(),
            "updated_at": ws.updated_at.isoformat(),
        }
        # "" must be OMITTED, never written: company_id keys the by-company-id
        # GSI and DynamoDB rejects the whole PutItem on an empty index key.
        if ws.company_id:
            item["company_id"] = ws.company_id
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
        del item["user_id"]
        on_tasks = self._meta_on_tasks()
        fields = [k for k in _WORKSPACE_OPTIONAL_FIELDS if not (on_tasks and old_row_only(k))]
        old_attrs = {k: item.pop(k) for k in list(item) if on_tasks and old_row_only(k)}
        attr = _camel if on_tasks else str
        names = {f"#f{i}": attr(k) for i, k in enumerate(item)}
        values = {f":f{i}": v for i, v in enumerate(item.values())}
        expr = "SET " + ", ".join(f"#f{i} = :f{i}" for i in range(len(item)))
        if on_tasks:
            expr += ", #mk = :mk, #wb = if_not_exists(#wb, :wb)"
            names.update({"#mk": "metaKind", "#wb": "workBackend"})
            values.update({":mk": "workspace", ":wb": "paperclip"})
        absent = [k for k in fields if k not in item]
        if absent:
            names.update({f"#r{i}": attr(k) for i, k in enumerate(absent)})
            expr += " REMOVE " + ", ".join(f"#r{i}" for i in range(len(absent)))
        kwargs: dict = {
            "UpdateExpression": expr,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": {**values, **(condition_values or {})},
        }
        if condition:
            kwargs["ConditionExpression"] = condition
        if not on_tasks:
            await self._ws_call("update_item", self._ws_key(ws.org_id), **kwargs)
        else:
            # Both rows in one transaction, so META never lands without company_id on the old row.
            old_fields = [k for k in _WORKSPACE_OPTIONAL_FIELDS if old_row_only(k) and k not in old_attrs]
            meta = {"TableName": self._ws_table().name, "Key": self._ws_key(ws.org_id), **_tasks_expressions(kwargs)}
            old = self._old_row_update(ws.org_id, ws.status, old_attrs, old_fields)
            try:
                await run_in_thread(
                    self._table().meta.client.transact_write_items,
                    TransactItems=[{"Update": meta}, {"Update": old}],
                )
            except ClientError as e:
                reasons = e.response.get("CancellationReasons") or []
                if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                    # Same error the single-table path raises, for the G2 mint site's handler.
                    raise ClientError(
                        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "put_workspace condition"}},
                        "UpdateItem",
                    ) from e
                raise
        publish_status_changed(ws.org_id)

    async def get_workspace(self, org_id: str, *, consistent_read: bool = False) -> Optional[PaperclipWorkspace]:
        resp = await self._ws_call("get_item", self._ws_key(org_id), ConsistentRead=consistent_read)
        item = resp.get("Item")
        # A missing ``status`` means the key names something other than a
        # workspace row (a pre-migration human row, a sentinel): not a workspace.
        if not item or "status" not in item:
            return None
        if self._meta_on_tasks():
            # company_id and the automation pause fence never leave the old row.
            old = await run_in_thread(self._table().get_item, Key={"user_id": org_id}, ConsistentRead=consistent_read)
            item.update({k: v for k, v in (old.get("Item") or {}).items() if old_row_only(k)})
        return self._item_to_workspace(item)

    @staticmethod
    def _item_to_workspace(item: dict) -> PaperclipWorkspace:
        return PaperclipWorkspace(
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
            automation_paused_at=item.get("automation_paused_at"),
            automation_pause_complete_at=item.get("automation_pause_complete_at"),
            # Keep an EMPTY map as {}: a carried-forward put must not drop it, or the
            # pause's per-routine nested SET has no parent to write into.
            automation_paused_routine_versions=dict(item["automation_paused_routine_versions"])
            if "automation_paused_routine_versions" in item
            else None,
            automation_pause_token=item.get("automation_pause_token"),
            automation_paused_agent_ids=set(item["automation_paused_agent_ids"])
            if item.get("automation_paused_agent_ids")
            else None,
            automation_paused_routine_ids=set(item["automation_paused_routine_ids"])
            if item.get("automation_paused_routine_ids")
            else None,
        )

    async def resolve_owner_row(
        self, owner_id: str, *, consistent_read: bool = False, include_inactive: bool = False
    ) -> Optional[PaperclipWorkspace]:
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
        """Record that ``user_id`` joined this workspace's Paperclip company.
        Conditional on the workspace row existing (never resurrects a partial)."""
        await self._ws_call(
            "update_item",
            self._ws_key(org_id),
            UpdateExpression="ADD member_user_ids :u SET updated_at = :t",
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeValues={":u": {user_id}, ":t": utc_now_iso()},
        )
        publish_status_changed(org_id)

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

    async def org_company_ids(self, org_id: str) -> set[str]:
        """Every ``company_id`` any row indexed under ``org_id`` names (the
        by-org-id GSI, projection ALL). Workspace rows carry ``org_id``; so do
        pre-migration per-member rows — which is what makes this the guard
        against minting a SECOND company for an org whose rows still await
        the one-off migration (PR #3097; the script lives outside the repo)."""
        ids: set[str] = set()
        async for page in self._query_pages(IndexName="by-org-id", KeyConditionExpression=Key("org_id").eq(org_id)):
            ids.update(item["company_id"] for item in page.get("Items", []) if item.get("company_id"))
        return ids

    async def find_workspace_by_company(self, company_id: str) -> Optional[PaperclipWorkspace]:
        """The ACTIVE workspace whose company is ``company_id``, via the
        ``by-company-id`` GSI (only workspace rows carry ``company_id``; the
        index projects ``status`` only, so the hit is re-read)."""
        async for page in self._query_pages(
            IndexName="by-company-id",
            KeyConditionExpression=Key("company_id").eq(company_id),
        ):
            for item in page.get("Items", []):
                # On tasks the old row's status is stale; the re-read below checks META's.
                if not self._meta_on_tasks() and item.get("status") != "active":
                    continue
                ws = await self.get_workspace(item["user_id"])
                if ws is not None and ws.status == "active":
                    return ws
        return None

    async def get_user_ids_for_company(self, company_id: str) -> list[str]:
        """The Clerk user ids who joined the active workspace owning
        ``company_id`` — the push audience for inbound Paperclip plugin
        events (``routers/paperclip_events.py``)."""
        ws = await self.find_workspace_by_company(company_id)
        return sorted(ws.member_user_ids) if ws is not None else []

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
        status: PaperclipCompanyStatus,
        last_error: Optional[str] = None,
        scheduled_purge_at: Optional[datetime] = None,
        not_if_status: Optional[PaperclipCompanyStatus] = None,
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

    async def record_paused_automation(
        self, org_id: str, *, agent_ids: list[str], routine_ids: list[str], token: str
    ) -> bool:
        """Stamp ``automation_paused_at`` (first pause wins), ADD what is about to be paused,
        and take the fence (``automation_pause_token``). Written BEFORE the Paperclip PATCHes,
        so a pause that dies halfway still leaves the resume a full list. Returns False when
        the row is gone (never resurrects a deleted workspace)."""
        expr = (
            "SET automation_paused_at = if_not_exists(automation_paused_at, :t), automation_pause_token = :k, "
            "automation_paused_routine_versions = if_not_exists(automation_paused_routine_versions, :empty)"
        )
        values: dict = {":t": utc_now_iso(), ":k": token, ":empty": {}}
        adds = []
        if agent_ids:
            adds.append("automation_paused_agent_ids :a")
            values[":a"] = set(agent_ids)
        if routine_ids:
            adds.append("automation_paused_routine_ids :r")
            values[":r"] = set(routine_ids)
        if adds:
            expr += " ADD " + ", ".join(adds)
        expr += " REMOVE automation_pause_complete_at"  # a new pause is incomplete until it finishes
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": org_id},
                UpdateExpression=expr,
                ConditionExpression="attribute_exists(user_id)",
                ExpressionAttributeValues=values,
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def pause_token_current(self, org_id: str, token: str) -> bool:
        """Whether the pause holding ``token`` still owns the row (no resume cleared it, no
        newer pause took it). Strongly consistent."""
        ws = await self.get_workspace(org_id, consistent_read=True)
        return ws is not None and ws.automation_pause_token == token

    async def _fenced_set(self, org_id: str, token: str, expr: str, names: dict, values: dict) -> bool:
        table = self._table()
        kwargs: dict = {
            "Key": {"user_id": org_id},
            "UpdateExpression": expr,
            "ConditionExpression": "automation_pause_token = :k",
            "ExpressionAttributeValues": {**values, ":k": token},
        }
        if names:
            kwargs["ExpressionAttributeNames"] = names
        try:
            await run_in_thread(table.update_item, **kwargs)
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def record_routine_version(self, org_id: str, *, token: str, routine_id: str, updated_at: str) -> bool:
        """Persist one paused routine's ``updatedAt`` the moment it is paused, so a pause
        that fails later still leaves resume the versions of what it already paused."""
        return await self._fenced_set(
            org_id,
            token,
            "SET automation_paused_routine_versions.#r = :v",
            {"#r": routine_id},
            {":v": updated_at},
        )

    async def mark_pause_complete(self, org_id: str, *, token: str) -> bool:
        """Every PATCH of this pause landed: the expired-trial sweep may stop retrying.
        Fenced: a pause a resume superseded never marks itself complete."""
        return await self._fenced_set(org_id, token, "SET automation_pause_complete_at = :t", {}, {":t": utc_now_iso()})

    async def clear_paused_automation(self, org_id: str, *, agent_ids: list[str], routine_ids: list[str]) -> None:
        """Undo ``record_paused_automation`` for exactly the ids a resume handled. DELETE
        (not REMOVE) on the sets, so ids a racing pause added after our read survive."""
        # ponytail: drops every version, not just these ids; a pause racing a resume then
        # falls back to the plain "still paused" check for its routines.
        # Removing the token is the fence: a pause still running sees it gone and stops.
        expr = (
            "REMOVE automation_paused_at, automation_pause_complete_at, automation_paused_routine_versions, "
            "automation_pause_token"
        )
        values: dict = {}
        deletes = []
        if agent_ids:
            deletes.append("automation_paused_agent_ids :a")
            values[":a"] = set(agent_ids)
        if routine_ids:
            deletes.append("automation_paused_routine_ids :r")
            values[":r"] = set(routine_ids)
        if deletes:
            expr += " DELETE " + ", ".join(deletes)
        kwargs: dict = {
            "Key": {"user_id": org_id},
            "UpdateExpression": expr,
            "ConditionExpression": "attribute_exists(user_id)",
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        table = self._table()
        try:
            await run_in_thread(table.update_item, **kwargs)
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return

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
                Item=self._ws_item(item, org_id, ts.META_SK),
                ConditionExpression="attribute_not_exists(user_id)",
            )
            publish_status_changed(org_id)
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def delete(self, key: str) -> None:
        await run_in_thread(
            self._table().delete_item,
            Key={"user_id": key},
        )
        if self._meta_on_tasks():
            await run_in_thread(self._ws_table().delete_item, Key=self._ws_key(key))

    # ------------------------------------------------------------------
    # Identity rows
    # ------------------------------------------------------------------

    async def put_identity(self, identity: PaperclipIdentity) -> None:
        """Whole-item put of a SETTLED identity row (no status, no claim
        attributes) — the write that confirms a sign-up."""
        now = datetime.now(timezone.utc)
        item = {
            "user_id": identity.user_id,
            "paperclip_user_id": identity.paperclip_user_id,
            "paperclip_password_encrypted": identity.paperclip_password_encrypted,
            "paperclip_email": identity.paperclip_email,
            "created_at": (identity.created_at or now).isoformat(),
            "updated_at": now.isoformat(),
        }
        await run_in_thread(self._table().put_item, Item=item)

    async def get_identity(self, user_id: str, *, consistent_read: bool = False) -> Optional[PaperclipIdentity]:
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": user_id},
            ConsistentRead=consistent_read,
        )
        item = resp.get("Item")
        if not item:
            return None
        return PaperclipIdentity(
            user_id=item["user_id"],
            paperclip_user_id=item.get("paperclip_user_id", ""),
            paperclip_password_encrypted=item.get("paperclip_password_encrypted", ""),
            paperclip_email=item.get("paperclip_email", ""),
            status=item.get("status"),
            created_at=_iso_or_none(item, "created_at"),
            updated_at=_iso_or_none(item, "updated_at"),
        )

    async def fail_signup_claim(self, user_id: str, *, reason: str, claim_id: str | None) -> bool:
        """Downgrade an UNCONFIRMED sign-up claim to ``failed`` so the next
        attempt can reclaim it. Never touches a confirmed identity
        (``paperclip_user_id`` set), and — when ``claim_id`` is given — never
        a newer holder's stolen claim. Returns True iff it landed."""
        condition = (
            "attribute_exists(user_id) AND (attribute_not_exists(paperclip_user_id) OR paperclip_user_id = :empty)"
        )
        values: dict = {":f": "failed", ":e": reason, ":u": utc_now_iso(), ":empty": ""}
        if claim_id is not None:
            condition += " AND (attribute_not_exists(seeding_claim_id) OR seeding_claim_id = :cid)"
            values[":cid"] = claim_id
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": user_id},
                UpdateExpression="SET #s = :f, last_error = :e, updated_at = :u",
                ConditionExpression=condition,
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues=values,
            )
            return True
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return False

    async def try_acquire_seed_claim(
        self,
        user_id: str,
        *,
        claim_id: str,
        stale_after_seconds: int = 300,
    ) -> bool:
        """Strongly-consistent "I am signing this human up" claim on their
        IDENTITY row — the fence against a duplicate Better Auth sign-up, taken
        by ``provision_org`` (the provisioner) and ``provision_member``.

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
        live on dev 2026-07-24, twice, 102ms and 358ms apart). This is a PK
        conditional write, so
        it is strongly consistent and cross-instance-safe, and it lives on the
        identity row so the loser can simply poll it.

        Claims retain staged encrypted signup credentials across retries and
        stale-lease takeover. No Paperclip user ID exists until signup or
        authenticated recovery returns. The claim names no org and no company:
        it is identity only, so it never enters the by-org-id or
        by-company-id indexes.

        Steal threshold rationale:
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
            "status": "seeding",
            "seeding_claim_id": claim_id,
            "seeding_claimed_at": now.isoformat(),
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
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

    async def get_agent_key(self, org_id: str, agent_id: str) -> str | None:
        """The plaintext Paperclip API key stored for ``agent_id``, or None when
        absent or undecryptable (an ``ENCRYPTION_KEY`` change; the key is re-mintable)."""
        resp = await run_in_thread(
            self._table().get_item,
            Key={"user_id": org_id},
            ProjectionExpression="agent_keys.#a",
            ExpressionAttributeNames={"#a": agent_id},
            ConsistentRead=True,
        )
        token = ((resp.get("Item") or {}).get("agent_keys") or {}).get(agent_id)
        if not token:
            return None
        try:
            return decrypt(token)
        except ValueError:
            logger.warning("paperclip_repo.get_agent_key: undecryptable key org=%s agent=%s", org_id, agent_id)
            return None

    async def put_agent_key(self, org_id: str, agent_id: str, plaintext: str) -> str | None:
        """Store ``agent_id``'s key, create-only, and return the key that is stored.

        A concurrent writer who stored first wins and its key is returned (the
        caller revokes its own duplicate). An undecryptable stored key is
        overwritten. None when ``org_id`` has no workspace row: a key with no
        row to live on is never written anywhere else.
        """
        table = self._table()
        exc = table.meta.client.exceptions.ConditionalCheckFailedException
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": org_id},
                UpdateExpression="SET agent_keys = :empty",
                ConditionExpression="attribute_exists(#s) AND attribute_not_exists(agent_keys)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":empty": {}},
            )
        except exc:
            pass
        kwargs = {
            "Key": {"user_id": org_id},
            "UpdateExpression": "SET agent_keys.#a = :v",
            "ExpressionAttributeNames": {"#s": "status", "#a": agent_id},
            "ExpressionAttributeValues": {":v": encrypt(plaintext)},
        }
        try:
            await run_in_thread(
                table.update_item,
                ConditionExpression="attribute_exists(#s) AND attribute_not_exists(agent_keys.#a)",
                **kwargs,
            )
            return plaintext
        except exc:
            winner = await self.get_agent_key(org_id, agent_id)
            if winner is not None:
                return winner
        # The stored key is undecryptable (overwrite it) or there is no row (this fails too).
        try:
            await run_in_thread(table.update_item, ConditionExpression="attribute_exists(#s)", **kwargs)
            return plaintext
        except exc:
            return None

    async def delete_agent_key(self, org_id: str, agent_id: str) -> None:
        """Drop ``agent_id``'s stored key (agent delete). Idempotent."""
        table = self._table()
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": org_id},
                UpdateExpression="REMOVE agent_keys.#a",
                ConditionExpression="attribute_exists(agent_keys)",
                ExpressionAttributeNames={"#a": agent_id},
            )
        except table.meta.client.exceptions.ConditionalCheckFailedException:
            return

    async def add_agent_cost_remainder(self, org_id: str, key: str, delta: int, *, need: int = 0) -> dict | None:
        """Atomically add ``delta`` microcents to ``agent_cost_remainder[key]``; return the whole map.

        Internal sub-cent carry for Paperclip cost events (Paperclip owns the budget and stores
        integer cents). ``need`` > 0 makes the write conditional on the bucket holding at least
        that much (the claim before a post, with a negative ``delta``). None when the row is missing
        or the condition fails.

        Uses ``SET #k = if_not_exists(#k, :zero) + :d``, NOT ``ADD``: DynamoDB's ``ADD`` action does
        not operate on a nested document path, so ``ADD agent_cost_remainder.#k`` throws a
        ValidationException on real DynamoDB (moto does not enforce this). The SET form is the
        supported atomic nested increment.
        """
        table = self._table()
        exc = table.meta.client.exceptions.ConditionalCheckFailedException
        try:
            await run_in_thread(
                table.update_item,
                Key={"user_id": org_id},
                UpdateExpression="SET agent_cost_remainder = :empty",
                ConditionExpression="attribute_exists(user_id) AND attribute_not_exists(agent_cost_remainder)",
                ExpressionAttributeValues={":empty": {}},
            )
        except exc:
            pass
        cond = "attribute_exists(user_id)"
        values: dict = {":d": delta, ":zero": 0}
        if need:
            cond += " AND agent_cost_remainder.#k >= :need"
            values[":need"] = need
        try:
            resp = await run_in_thread(
                table.update_item,
                Key={"user_id": org_id},
                UpdateExpression="SET agent_cost_remainder.#k = if_not_exists(agent_cost_remainder.#k, :zero) + :d",
                ConditionExpression=cond,
                ExpressionAttributeNames={"#k": key},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except exc:
            return None
        return resp["Attributes"]["agent_cost_remainder"]

    async def prune_agent_cost_remainder(self, org_id: str, keys: list[str]) -> None:
        if not keys:
            return
        names = {f"#k{i}": k for i, k in enumerate(keys)}
        await run_in_thread(
            self._table().update_item,
            Key={"user_id": org_id},
            UpdateExpression="REMOVE " + ", ".join(f"agent_cost_remainder.{n}" for n in names),
            ExpressionAttributeNames=names,
        )

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

    async def record_onboarding_ids(
        self,
        org_id: str,
        *,
        onboarding_generation: str,
        goal_id: str,
        project_id: str,
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
        await self._ws_call(
            "update_item",
            self._ws_key(org_id),
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
        publish_status_changed(org_id)

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
        """Complete the generation after its goal, project, and BRIEF.md exist."""
        table = self._ws_table()
        try:
            await self._ws_call(
                "update_item",
                self._ws_key(org_id),
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
        publish_status_changed(org_id)
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

        table = self._ws_table()
        item = self._ws_item(item, owner_id, ts.PENDING_BRIEF_SK)
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
        """``{org_id: member_user_ids}`` for every ACTIVE workspace row (a scan).

        ponytail: a full-table scan per call; fine while the daily-digest pass
        is its only caller at a few per hour. Add a GSI if the table grows past
        a few thousand rows.
        """
        from boto3.dynamodb.conditions import Attr

        out: dict[str, set[str]] = {}
        if self._meta_on_tasks():
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
        kwargs: dict = {
            "FilterExpression": Attr("user_id").begins_with("org_") & Attr("status").eq("active"),
            "ProjectionExpression": "user_id, member_user_ids",
        }
        while True:
            resp = await run_in_thread(self._table().scan, **kwargs)
            for item in resp.get("Items", []):
                out[item["user_id"]] = set(item.get("member_user_ids") or ())
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                return out
            kwargs["ExclusiveStartKey"] = last_key

    async def scan_purge_due(self, now: datetime) -> list[PaperclipWorkspace]:
        """Disabled workspace rows whose ``scheduled_purge_at <= now``, via the
        by-status-purge-at GSI (KEYS_ONLY, so each hit is re-read)."""
        from boto3.dynamodb.conditions import Attr

        out: list[PaperclipWorkspace] = []
        if self._meta_on_tasks():
            pages = self._query_pages(
                self._ws_table(),
                IndexName="ws-meta",
                KeyConditionExpression=Key("metaKind").eq("workspace"),
                FilterExpression=Attr("status").eq("disabled") & Attr("scheduledPurgeAt").lte(now.isoformat()),
                ProjectionExpression="pk",
            )
        else:
            pages = self._query_pages(
                IndexName="by-status-purge-at",
                KeyConditionExpression=(Key("status").eq("disabled") & Key("scheduled_purge_at").lte(now.isoformat())),
            )
        async for page in pages:
            for item in page.get("Items", []):
                org_id = item["pk"].removeprefix("WS#") if "pk" in item else item["user_id"]
                ws = await self.get_workspace(org_id)
                if ws is not None:
                    out.append(ws)
                else:
                    logger.warning(
                        "paperclip_repo.scan_purge_due: GSI returned %s but base item missing",
                        org_id,
                    )
        return out


async def customer_ops_agent_id(owner_id: str) -> str | None:
    """The workspace's Customer Operations starter agent (the front desk), or None when the roster has none."""
    row = await PaperclipRepo(table_name="paperclip-companies").resolve_owner_row(owner_id)
    return (getattr(row, "starter_agent_ids", None) or {}).get("customer_operations")
