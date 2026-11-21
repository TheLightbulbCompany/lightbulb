"""The workspace's cloud member: the engine sidecar in the AgentCore microVM that holds the
workspace's memory log (``memory.*`` ops, ``memory_api`` and ``memory_sync``).

The backend owns two inputs the sidecar reads: the member key (in the boot secret, written only by
``agentcore_runtime.write_boot_secret``) and ``<owner>/network/sessions.json``, one address per
roster agent and ``members``, the workspace's people. The sidecar starts only when that file exists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid

from core.config import settings
from core.dynamodb import run_in_thread
from core.services import container_rpc
from core.services.messaging import ApiProblem

logger = logging.getLogger(__name__)

MAX_SESSIONS = 100


def sessions_key(owner_id: str) -> str:
    return f"{owner_id}/network/sessions.json"


def network_prefix(owner_id: str) -> str:
    return f"{owner_id}/network/"


async def sessions_exist(owner_id: str) -> bool:
    from core.services.hermes_profiles import owner_object_size

    return await run_in_thread(owner_object_size, sessions_key(owner_id)) is not None


async def write_sessions(owner_id: str) -> dict:
    """Write ``sessions.json``: one address per roster agent, and the workspace's people."""
    from core.services import clerk_admin
    from core.services.company_identity import slugify
    from core.services.hermes_profiles import put_owner_object
    from core.services.messaging import load_agent_roster
    from core.services.website_logins import _humans_to_ask

    roster, org, members = await asyncio.gather(
        load_agent_roster(owner_id), clerk_admin.get_organization(owner_id), _humans_to_ask(owner_id, None)
    )
    if len(roster) > MAX_SESSIONS:
        raise ApiProblem("INVALID_REQUEST", f"This workspace has reached {MAX_SESSIONS} network addresses.")

    # ponytail: names are claimed by agent id order, so a new duplicate-named agent with a lower id
    # can move an existing agent's address. A taken name's suffix grows (-<id[:4]>, -id<id[:8]>,
    # -id<id>) until unique. Stable addresses need a stored name per agent.
    used: set[str] = set()
    sessions = []
    for agent in roster:
        base = slugify(agent["name"]) or agent["id"][:8]
        name = base
        for suffix in (agent["id"][:4], f"id{agent['id'][:8]}", f"id{agent['id']}"):
            if name not in used:
                break
            name = f"{base}-{suffix}"
        used.add(name)
        sessions.append({"name": name, "agent": agent["name"] or name, "title": agent["name"], "agentId": agent["id"]})

    await _arm_wake(owner_id)
    # ``members``: the sidecar's gate, the workspace's people.
    doc = {"workspace": (org or {}).get("name") or owner_id, "sessions": sessions, "members": members}
    await run_in_thread(put_owner_object, sessions_key(owner_id), json.dumps(doc).encode())
    return doc


async def publish_sessions(owner_id: str) -> None:
    """When this workspace has a member, rewrite ``sessions.json`` and tell a warm sidecar.
    Best effort: never raises."""
    if not settings.OWNER_STATE_BUCKET:
        return
    try:
        if not await sessions_exist(owner_id):
            return
        await write_sessions(owner_id)
        # Only a warm sidecar is pinged: a parked one reads sessions.json at its next start,
        # and an invoke here would cold-start the workspace (~55 s) from an agent edit.
        if await _session_is_warm(owner_id):
            await container_rpc.call(owner_id, "network.sessions", {})
            await drain_ops(owner_id)  # the member is up and just refreshed: send what waited for it
    except Exception:  # noqa: BLE001
        logger.warning("network sessions publish failed for owner=%s", owner_id, exc_info=True)


async def _session_is_warm(owner_id: str) -> bool:
    """The ``__session__#<owner>`` estimate ``runtime_transport`` keeps, for THIS runtime.
    Includes the conservative drain window, while an old microVM may still be serving. False with
    no runtime yet."""
    from core.repositories import container_repo
    from core.services.runtime_transport import LIVE_GRACE_S, runtime_arn

    container, row = await asyncio.gather(
        container_repo.get_by_owner_id(owner_id), run_in_thread(container_repo.get_session_sync, owner_id)
    )
    arn = row.get("arn")
    desired = runtime_arn(container or {})
    # The ledger is authoritative even when a legacy workspace reached the pool before
    # its route marker was saved and the tier setting has since been rolled back.
    draining = bool(arn and desired and arn != desired)
    warm_after = time.time() - (LIVE_GRACE_S if draining else 0)
    return bool(arn and desired and (arn == desired or draining)) and int(row.get("warm_until") or 0) > warm_after


# ---------------------------------------------------------------------------
# wake: a parked workspace's owed memory forwards start its runtime
# ---------------------------------------------------------------------------

WAKE_WINDOW_S = 10


def _netwake_key(owner_id: str) -> dict:
    from core.repositories import messaging_repo

    return {"pk": messaging_repo.owner_pk(owner_id), "sk": messaging_repo.netwake_sk()}


async def _arm_wake(owner_id: str) -> None:
    """The ``NETWAKE`` row ``wake`` reads: its ``tokenHash`` is set once and kept, so ``_queue_wake``
    can condition on it."""
    from core.repositories import messaging_repo

    await run_in_thread(
        messaging_repo._table().update_item,
        Key=_netwake_key(owner_id),
        UpdateExpression="SET tokenHash = if_not_exists(tokenHash, :h)",
        ExpressionAttributeValues={":h": secrets.token_hex(32)},
    )


async def wake(owner_id: str) -> bool:
    """Queue a ``network_wake`` for this workspace (rate-limited by ``_queue_wake``). False when it
    has no member."""
    from core.repositories import messaging_repo

    key = _netwake_key(owner_id)
    row = await messaging_repo._get(key["pk"], key["sk"])
    if not row or not row.get("tokenHash"):
        return False
    await _queue_wake(owner_id, str(row["tokenHash"]))
    return True


async def _queue_wake(owner_id: str, digest: str) -> None:
    """At most one ``network_wake`` per workspace per ``WAKE_WINDOW_S`` reaches the work queue."""
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo

    key = _netwake_key(owner_id)
    now = int(time.time())
    ulid = f"{now * 1000:012x}{uuid.uuid4().hex[:20]}"
    table_name = messaging_repo._table().name
    try:
        await messaging_repo._transact(
            [
                {
                    "Update": {
                        "TableName": table_name,
                        "Key": key,
                        "UpdateExpression": "SET lastWakeAt = :now",
                        # tokenHash too: a reconnect that deleted the row in between must not see it recreated.
                        # lastWakeAt is each backend task's own clock, so skew can admit a second wake
                        # inside the window; that costs one extra no-op network.wake RPC.
                        "ConditionExpression": "tokenHash = :h AND (attribute_not_exists(lastWakeAt) OR lastWakeAt < :cutoff)",
                        "ExpressionAttributeValues": {":now": now, ":h": digest, ":cutoff": now - WAKE_WINDOW_S},
                    }
                },
                {
                    "Put": {
                        "TableName": table_name,
                        "Item": {
                            "pk": key["pk"],
                            "sk": messaging_repo.work_netwake_sk(ulid),
                            "payload": {"kind": "network_wake", "ownerId": owner_id},
                            "messageGroupId": f"netwake#{owner_id}",
                            "messageDeduplicationId": ulid,
                            "expiresAt": now + messaging_repo.OUTBOX_TTL_S,
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        if not messaging_repo.is_cancelled(exc):
            raise
        logger.info("network wake skipped (within %ss) owner=%s", WAKE_WINDOW_S, owner_id)
        return
    logger.info("network wake queued owner=%s", owner_id)


# ---------------------------------------------------------------------------
# cloud-member ops (memory.*): only while the runtime is up
# ---------------------------------------------------------------------------


async def call_op(owner_id: str, op: str, params: dict, timeout: float = 30.0) -> dict | None:
    """One cloud-member op over the runtime RPC path (``container_rpc.call``). ``None`` when the
    runtime is parked: this never cold-starts it (an invoke would, ~55 s)."""
    if not await _session_is_warm(owner_id):
        return None
    return await container_rpc.call(owner_id, op, params, timeout_s=timeout)


def _op_pk(owner_id: str) -> str:
    from core.repositories import messaging_repo

    return messaging_repo.owner_pk(owner_id)


async def queue_op(owner_id: str, op: str, params: dict) -> None:
    """Persist an op for ``drain_ops`` (run when the runtime is next up). Params are JSON text."""
    from core.repositories import messaging_repo

    sk = f"MEMOP#{int(time.time() * 1000):013d}#{uuid.uuid4().hex[:8]}"
    # Params can carry fact text: a runtime parked past the window loses the op, not the privacy.
    expires = int(time.time()) + OP_TTL_S
    item = {"pk": _op_pk(owner_id), "sk": sk, "op": op, "params": json.dumps(params), "expiresAt": expires}
    await run_in_thread(messaging_repo._table().put_item, Item=item)


_MAX_ATTEMPTS = 5
_CLAIM_S = 120
OP_TTL_S = 30 * 86400  # a queued op's lifetime (``expiresAt``: the messaging table's TTL attribute)


async def drain_ops(owner_id: str) -> int:
    """Send queued ops in order. Never raises (it runs on the edit path); returns how many were
    sent. Each row is claimed with a conditional write before it is sent, so two drains cannot
    both send it. An op that fails is retried on later drains, up to ``_MAX_ATTEMPTS``, then
    dropped with a warning naming the op and owner (never the params)."""
    try:
        return await _drain(owner_id)
    except Exception as exc:  # noqa: BLE001 -- params can carry fact text: log the class only
        logger.warning("queued ops drain failed owner=%s error=%s", owner_id, type(exc).__name__)
        return 0


async def _drain(owner_id: str) -> int:
    from boto3.dynamodb.conditions import Key
    from botocore.exceptions import ClientError

    from core.repositories import messaging_repo

    table = messaging_repo._table()
    query = {"KeyConditionExpression": Key("pk").eq(_op_pk(owner_id)) & Key("sk").begins_with("MEMOP#")}
    rows: list[dict] = []
    while True:
        page = await run_in_thread(table.query, **query)
        rows += page["Items"]
        if not page.get("LastEvaluatedKey"):
            break
        query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    sent = 0
    for row in rows:
        key = {"pk": row["pk"], "sk": row["sk"]}
        now = int(time.time())
        try:
            await run_in_thread(
                table.update_item,
                Key=key,
                UpdateExpression="SET claimed_until = :u",
                ConditionExpression="attribute_exists(pk) AND (attribute_not_exists(claimed_until) OR claimed_until < :now)",
                ExpressionAttributeValues={":u": now + _CLAIM_S, ":now": now},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # another drain has it, or it is already sent
            raise
        failed = False
        try:
            answer = await call_op(owner_id, row["op"], json.loads(row["params"]), 30.0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("queued op failed owner=%s op=%s error=%s", owner_id, row["op"], type(exc).__name__)
            answer, failed = {}, True
        if answer is None:  # parked: leave the rest for the next drain
            await run_in_thread(table.update_item, Key=key, UpdateExpression="REMOVE claimed_until")
            break
        if not failed:
            await run_in_thread(table.delete_item, Key=key)
            sent += 1
            continue
        attempts = int(row.get("attempts") or 0) + 1
        if attempts >= _MAX_ATTEMPTS:
            logger.warning("queued op dropped after %d attempts owner=%s op=%s", attempts, owner_id, row["op"])
            await run_in_thread(table.delete_item, Key=key)
        else:
            await run_in_thread(
                table.update_item,
                Key=key,
                UpdateExpression="SET attempts = :a REMOVE claimed_until",
                ExpressionAttributeValues={":a": attempts},
            )
    return sent
