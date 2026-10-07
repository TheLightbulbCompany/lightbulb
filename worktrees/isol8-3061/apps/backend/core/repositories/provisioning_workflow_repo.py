"""One durable row per provisioning action.

Stripe's Provisioning API has no webhooks and "doesn't define a caller
idempotency contract for state-changing requests", so this row IS the
integration's memory: "A browser session, Provider redirect, worker restart, or
deployment failure must not lose an Account request ID, Resource ID, or
uncertain write state" (guide) — restated in spec §3.5. Nothing sweeps these
rows in the background: a workflow moves when somebody reads it.

Fields follow the guide's own Provisioning-workflow record (workflow_id,
tenant_id, action, account_request_id, resource_id, provider_id, service_ref,
status, created_at, updated_at) plus what spec §3.2 needs: the approval
snapshot, the project id, and a SAFE error code.

Secrets never land here. Not the access configuration, not a checkout URL, not
a deep link, not a redirect URL.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

# Our states, not Stripe's (spec §3.2; the guide's example model is the same
# shape). Stripe's own enums stay in code font wherever they appear.
STATES = (
    "ask",
    "awaiting_approval",
    "connecting_provider",
    "awaiting_user_action",
    # The owner never came back. NON-terminal on purpose: the ids are still
    # good and the next click re-mints the link (see the connect-link route).
    "stale",
    "provisioning",
    "binding",
    "active",
    "failed",
    "requires_review",
    # The owner said no, or withdrew the ask before anything was created.
    "rejected",
    "cancelled",
)
TERMINAL_STATES = frozenset({"active", "failed", "requires_review", "rejected", "cancelled"})

# States where no state-changing Stripe POST can have happened yet, so the ask
# can simply be withdrawn. `awaiting_user_action` is conditional — the route
# checks for an account_request_id / resource_id as well.
CANCELLABLE_STATES = frozenset({"ask", "awaiting_approval", "awaiting_user_action", "stale"})

# The claim is written only by `claim`/`release` (and dropped by a terminal
# `transition`). A `patch` able to set it could hand one reader a row another
# is mid-step on, which is the whole thing the claim exists to prevent.
_PROTECTED_FIELDS = frozenset({"workflow_id", "owner_id", "state", "created_at", "lease_owner", "lease_until"})


def _table():
    return get_table("provisioning-workflows")


async def create(
    *,
    owner_id: str,
    action: str,
    provider_id: str,
    provider_name: str,
    service_ref: str,
    service_id: str,
    configuration: dict,
    issue_id: str | None = None,
    reason: str = "",
) -> dict:
    """Persist the intent BEFORE any Stripe write (guide: "Persist the intent
    to create or change a Resource before making the request")."""
    now = utc_now_iso()
    item = {
        "workflow_id": f"pwf_{uuid.uuid4().hex}",
        "owner_id": owner_id,
        "action": action,
        "state": "ask",
        "provider_id": provider_id,
        "provider_name": provider_name,
        "service_ref": service_ref,
        "service_id": service_id,
        "configuration": configuration or {},
        "issue_id": issue_id,
        # The asking agent's own sentence, kept so a re-filed card after a
        # catalog drift still says why — the row is the memory, and the reason
        # is not recoverable from the catalog.
        "reason": reason or "",
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }
    await run_in_thread(_table().put_item, Item=item)
    return item


async def get(workflow_id: str) -> dict | None:
    resp = await run_in_thread(_table().get_item, Key={"workflow_id": workflow_id})
    return resp.get("Item")


async def list_for_owner(owner_id: str) -> list[dict]:
    resp = await run_in_thread(
        _table().query, IndexName="owner-state-index", KeyConditionExpression=Key("owner_id").eq(owner_id)
    )
    return resp.get("Items", [])


async def transition(workflow_id: str, *, from_state: str, to_state: str, fields: dict | None = None) -> dict | None:
    """Move the row, conditionally on it still being in ``from_state``.

    ``None`` means someone else moved it first — two concurrent readers, or two
    approvals. The caller must treat that as "not mine any more" and stop,
    never as a failure to retry.
    """
    if to_state not in STATES:
        raise ValueError(f"unknown state: {to_state}")
    now = utc_now_iso()
    sets = ["#st = :to", "updated_at = :now"]
    names = {"#st": "state"}
    values = {":to": to_state, ":now": now, ":from": from_state}
    for i, (key, value) in enumerate(sorted((fields or {}).items())):
        if key in _PROTECTED_FIELDS:
            raise ValueError(f"{key} cannot be set through transition fields")
        sets.append(f"#f{i} = :f{i}")
        names[f"#f{i}"] = key
        values[f":f{i}"] = value
    expr = "SET " + ", ".join(sets)
    if to_state in TERMINAL_STATES:
        # Drop the claim: nothing steps a terminal row, so a lingering one
        # would only make the next reader wait out a ttl before confirming
        # there is nothing to do.
        expr += " REMOVE lease_owner, lease_until"
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"workflow_id": workflow_id},
            UpdateExpression=expr,
            ConditionExpression="attribute_exists(workflow_id) AND #st = :from",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


async def patch(workflow_id: str, fields: dict) -> dict | None:
    """Write non-state fields (ids, attempts, next_poll_at). State moves only
    through ``transition`` so every state change is conditional."""
    if not fields:
        return await get(workflow_id)
    sets = ["updated_at = :now"]
    names: dict[str, str] = {}
    values: dict = {":now": utc_now_iso()}
    for i, (key, value) in enumerate(sorted(fields.items())):
        if key in _PROTECTED_FIELDS:
            raise ValueError(f"{key} cannot be set through patch")
        sets.append(f"#f{i} = :f{i}")
        names[f"#f{i}"] = key
        values[f":f{i}"] = value
    resp = await run_in_thread(
        _table().update_item,
        Key={"workflow_id": workflow_id},
        UpdateExpression="SET " + ", ".join(sets),
        ConditionExpression="attribute_exists(workflow_id)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes")


async def claim(workflow_id: str, *, owner: str, ttl_s: int) -> dict | None:
    """Take this row's step claim. ``None`` when someone else holds it.

    One step is a READ plus a write, not a single conditional write — so the
    conditional ``transition`` alone still lets two concurrent readers (or a
    rolling deploy's two instances, ``maxHealthyPercent: 200``) poll Stripe for
    the same workflow and file the same card. The claim elects one stepper per
    row; the other reports the row as it stands.

    There is deliberately NO renewal clause. The backend runs
    ``desiredCount: 1``, so every reader in a process shares one ``owner``
    string — an ``OR #lo = :me`` would hand the row to the second concurrent
    read of the same process, which is exactly the double-create this exists to
    stop. A holder that wants the row again waits out the ttl like anyone else.

    Time-bounded and never released by a crash: a reader that dies mid-step
    simply lets its claim expire, and the next read takes the row over.

    ``updated_at`` is deliberately untouched — taking a claim is not a change
    to the workflow.
    """
    now = utc_now_iso()
    until = (datetime.now(timezone.utc) + timedelta(seconds=ttl_s)).isoformat()
    try:
        resp = await run_in_thread(
            _table().update_item,
            Key={"workflow_id": workflow_id},
            UpdateExpression="SET #lo = :me, #lu = :until",
            ConditionExpression=("attribute_exists(workflow_id) AND (attribute_not_exists(#lu) OR #lu < :now)"),
            ExpressionAttributeNames={"#lo": "lease_owner", "#lu": "lease_until"},
            ExpressionAttributeValues={":me": owner, ":until": until, ":now": now},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


async def release(workflow_id: str, *, owner: str) -> None:
    """Hand the row back. Silent when the claim is not ours (or gone):
    releasing what we do not hold is a no-op, never a theft."""
    try:
        await run_in_thread(
            _table().update_item,
            Key={"workflow_id": workflow_id},
            UpdateExpression="REMOVE #lo, #lu",
            ConditionExpression="#lo = :me",
            ExpressionAttributeNames={"#lo": "lease_owner", "#lu": "lease_until"},
            ExpressionAttributeValues={":me": owner},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
