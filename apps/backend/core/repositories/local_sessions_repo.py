"""Local harness registry in the messaging table; directory rows never grant access.

LOCALUSER#<scope>#<human> / DEVICE#<mac> and SESSION#<id>
LOCALSESSION#<scope>#<id> / META, EVENT#<20-digit seq>, COMMAND#<requestId>, AUDIT#<ms>#<uuid>, REPLY#<requestId>
No TTL on command/audit history; pending room replies expire after 24 hours.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Sequence

from core.repositories import container_repo, messaging_repo as store


def user_pk(owner_id: str, user_id: str) -> str:
    return f"LOCALUSER#{owner_id}#{user_id}"


def session_pk(owner_id: str, session_id: str) -> str:
    return f"LOCALSESSION#{owner_id}#{session_id}"


def session_id(owner_id: str, device_id: str, provider: str, profile: str, conversation: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, store.request_hash(owner_id, device_id, provider, profile, conversation)))


async def get_enrollment(owner_id: str, user_id: str, device_id: str) -> dict | None:
    return await store._get(user_pk(owner_id, user_id), f"DEVICE#{device_id}")


async def list_enrollments(owner_id: str, user_id: str) -> list[dict]:
    return await store._query(user_pk(owner_id, user_id), begins_with="DEVICE#")


async def get_session(owner_id: str, local_session_id: str) -> dict | None:
    return await store._get(session_pk(owner_id, local_session_id), "META")


async def get_sessions(owner_id: str, local_session_ids: list[str]) -> list[dict | None]:
    """Many META rows in one strong read per 100, in the order asked; None where there is none."""
    keys = [(session_pk(owner_id, sid), "META") for sid in local_session_ids]
    found = await store.batch_get(keys)
    return [found.get(key) for key in keys]


async def list_user_sessions(owner_id: str, user_id: str) -> list[dict]:
    return await store._query(user_pk(owner_id, user_id), begins_with="SESSION#")


async def get_command(owner_id: str, local_session_id: str, request_id: str) -> dict | None:
    return await store._get(session_pk(owner_id, local_session_id), f"COMMAND#{request_id}")


async def list_events(
    owner_id: str, local_session_id: str, after_seq: int, limit: int
) -> tuple[list[dict], int | None]:
    rows = await store._query(
        session_pk(owner_id, local_session_id),
        between=(f"EVENT#{after_seq + 1:020d}", "EVENT#99999999999999999999"),
        limit=limit + 1,
    )
    return rows[:limit], int(rows[limit - 1]["seq"]) if len(rows) > limit else None


def pairing_condition(
    owner_id: str,
    user_id: str,
    device_id: str,
    cli_device_id: str | None,
) -> dict:
    names = {"#pd": "paired_devices", "#mac": device_id, "#uid": "userId", "#client": "clientId"}
    values = {":user": user_id, ":mac": "openclaw-macos"}
    expression = "#pd.#mac.#uid = :user AND #pd.#mac.#client = :mac"
    if cli_device_id:
        names["#cli"] = cli_device_id
        values[":cli"] = "node-host"
        expression += " AND #pd.#cli.#uid = :user AND #pd.#cli.#client = :cli"
    return {
        "ConditionCheck": {
            "TableName": container_repo._get_table().name,
            "Key": {"owner_id": owner_id},
            "ConditionExpression": expression,
            "ExpressionAttributeNames": names,
            "ExpressionAttributeValues": values,
        }
    }


def enrollment_condition(enrollment: dict) -> dict:
    return {
        "ConditionCheck": {
            "TableName": store._table().name,
            "Key": {"pk": enrollment["pk"], "sk": enrollment["sk"]},
            "ConditionExpression": "enabled = :yes AND revision = :revision",
            "ExpressionAttributeValues": {":yes": True, ":revision": enrollment["revision"]},
        }
    }


def native_source_condition(source: dict) -> dict:
    """Fence tool writes on authority changes without racing transcript/heartbeat revisions."""
    condition = {
        "ConditionCheck": {
            "TableName": store._table().name,
            "Key": {"pk": source["pk"], "sk": "META"},
            "ConditionExpression": (
                "generation = :generation AND connectionId = :connection AND deviceId = :device "
                "AND humanUserId = :human AND provider = :provider AND nativeConversationId = :conversation "
                "AND onlineUntil > :now AND attribute_not_exists(endedAt) "
                "AND (attribute_not_exists(unshared) OR unshared = :no)"
            ),
            "ExpressionAttributeValues": {
                ":generation": source["generation"],
                ":connection": source["connectionId"],
                ":device": source["deviceId"],
                ":human": source["humanUserId"],
                ":provider": source["provider"],
                ":conversation": source["nativeConversationId"],
                ":now": int(time.time()),
                ":no": False,
            },
        }
    }
    if source["provider"] == "codex":
        if not source.get("activeTurnId"):
            raise ValueError("Codex source turn is unavailable")
        condition["ConditionCheck"]["ConditionExpression"] += " AND activeTurnId = :turn"
        condition["ConditionCheck"]["ExpressionAttributeValues"][":turn"] = source["activeTurnId"]
    return condition


async def put_session(row: dict, previous: dict | None, enrollment: dict, *, extra: Sequence[dict] = ()) -> None:
    """CAS preserves concurrent command ordering; canonical and directory land together."""
    put = {"TableName": store._table().name, "Item": row, "ConditionExpression": "attribute_not_exists(sk)"}
    if previous:
        put.update(
            ConditionExpression="revision = :revision", ExpressionAttributeValues={":revision": previous["revision"]}
        )
    projection = {key: row[key] for key in ("ownerId", "humanUserId", "deviceId", "sessionId")}
    projection.update(pk=user_pk(row["ownerId"], row["humanUserId"]), sk=f"SESSION#{row['sessionId']}")
    # WORKSPACE#<ws>/SESSION#<sid> exists exactly while the session is shared: the AppSync handler
    # admits a member to /ws/<ws>/session-frames/<sid> only then (it cannot build this row's key,
    # which needs the owner). In the same transaction as the row, so the two never disagree.
    marker = {
        "pk": store.workspace_pk(await store.ensure_workspace_id(row["ownerId"])),
        "sk": f"SESSION#{row['sessionId']}",
    }
    shared = not row.get("unshared") and row.get("removedAt") is None
    marker_write = (
        {"Put": {"TableName": store._table().name, "Item": {**marker, "ownerId": row["ownerId"]}}}
        if shared
        else {"Delete": {"TableName": store._table().name, "Key": marker}}
    )
    await container_repo.retry_conflict(
        lambda: store._transact(
            [
                pairing_condition(row["ownerId"], row["humanUserId"], row["deviceId"], None),
                enrollment_condition(enrollment),
                {"Put": put},
                {"Put": {"TableName": store._table().name, "Item": projection}},
                marker_write,
            ]
            + list(extra)
        )
    )


def room_conditions(owner: str, human: str, receipt: dict, *, include_meta: bool = True) -> list[dict]:
    """Fence room-originated commands at admission and delivery against both human memberships."""
    room = receipt.get("roomId")

    def active_stream(user: str) -> dict:
        # Membership removal deactivates this stream; fence the final dispatch on it.
        return {
            "ConditionCheck": {
                "TableName": store._table().name,
                "Key": {"pk": store.owner_pk(owner), "sk": store.userstream_sk(user)},
                "ConditionExpression": "active = :active",
                "ExpressionAttributeValues": {":active": True},
            }
        }

    # An agent reaches sessions across the workspace, not through the owner's room seat, so the
    # session owner must still be a member (spec 2026-10-07-agent-session-approval).
    owner_check = [active_stream(human)] if (receipt.get("actor") or {}).get("kind") == "agent" else []
    if not room:
        # An offline teammate's old token is not authority at delivery time.
        sender = receipt.get("actorUserId")
        if receipt.get("offlineIdentity") and sender != human:
            return [active_stream(sender)]
        return owner_check
    table = store._table().name
    actor = ("agent", receipt["actor"]["id"]) if "actor" in receipt else ("human", receipt["actorUserId"])
    meta_check = (
        [
            {
                "ConditionCheck": {
                    "TableName": table,
                    "Key": {"pk": store.room_pk(room), "sk": "META"},
                    "ConditionExpression": "ownerId = :owner",
                    "ExpressionAttributeValues": {":owner": owner},
                }
            }
        ]
        if include_meta
        else []
    )
    return (
        meta_check
        + owner_check
        + [
            {
                "ConditionCheck": {
                    "TableName": table,
                    "Key": {"pk": store.room_pk(room), "sk": store.member_sk(kind, user)},
                    "ConditionExpression": "attribute_exists(sk)",
                }
            }
            # An agent reaches any shared session in the workspace (spec 2026-10-07-agent-session-approval):
            # only its own seat in the room it acts from is fenced, not the session owner's.
            for kind, user in sorted({actor} if actor[0] == "agent" else {("human", human), actor})
        ]
    )


async def list_commands(owner: str, sid: str) -> list[dict]:
    # ponytail: per-session receipt history scan; add an ordered pending index if measured volume warrants it.
    return sorted(await store._query(session_pk(owner, sid), begins_with="COMMAND#"), key=lambda row: row["commandSeq"])


async def admit_command(
    row: dict,
    enrollment: dict,
    receipt: dict,
    *,
    allow_offline: bool = False,
    extra: Sequence[dict] = (),
    room_meta_fenced_by_extra: bool = False,
) -> None:
    owner, human, device = row["ownerId"], row["humanUserId"], row["deviceId"]
    table = store._table().name
    ending = receipt.get("kind") == "end"
    await store._transact(
        room_conditions(owner, human, receipt, include_meta=not room_meta_fenced_by_extra)
        + [
            pairing_condition(owner, human, device, None),
            enrollment_condition(enrollment),
            {
                "Update": {
                    "TableName": table,
                    "Key": {"pk": row["pk"], "sk": "META"},
                    # `endAskedAt`: the owner's End is on its way, so the public watch stops now
                    # (`session_screen.forward`), before the provider prints its exit lines.
                    "UpdateExpression": "SET commandSeq = :next, revision = :newRevision"
                    + (", endAskedAt = :now" if ending else ""),
                    "ConditionExpression": "revision = :revision AND generation = :generation AND attribute_not_exists(endedAt)"
                    + ("" if allow_offline else " AND onlineUntil > :now"),
                    "ExpressionAttributeValues": {
                        ":next": receipt["commandSeq"],
                        ":revision": row["revision"],
                        ":newRevision": str(uuid.uuid4()),
                        ":generation": receipt["expectedGeneration"],
                        **({} if allow_offline and not ending else {":now": receipt["createdAt"]}),
                    },
                }
            },
            {"Put": {"TableName": table, "Item": receipt, "ConditionExpression": "attribute_not_exists(sk)"}},
            *extra,
        ]
    )


async def delete_owner(owner_id: str) -> int:
    """Called by the account wipe after pairing authority has been removed."""
    from boto3.dynamodb.conditions import Attr

    from core.services import session_pickup

    # Raw transcripts and code a teammate allowed to be picked up, outside the <owner>/ prefix.
    await store.run_in_thread(session_pickup.delete_prefix, f"{session_pickup.PREFIX}/{owner_id}/")
    table = store._table()
    # ponytail: deletion-only table scan; add a catalog if measured wipe volume warrants it.
    kwargs = {
        "FilterExpression": Attr("pk").begins_with(f"LOCALUSER#{owner_id}#")
        | Attr("pk").begins_with(f"LOCALSESSION#{owner_id}#"),
        "ProjectionExpression": "pk, sk, shareLink.tokenHash, copyId, itemPk",
        "ConsistentRead": True,
    }
    count = 0
    while True:
        response = await store.run_in_thread(table.scan, **kwargs)
        rows = response.get("Items", [])
        # External rows the partitions point at (no TTL): the link lookup, each copy's code row and
        # its session-copies/ objects. Deleted with the page that holds the pointer.
        extra = []
        for row in rows:
            if row["sk"] == "META" and row.get("shareLink"):
                extra.append({"pk": f"SHARELINK#{row['shareLink']['tokenHash']}", "sk": "META"})
            if row["sk"].startswith("COPY#") and row.get("copyId"):
                await store.run_in_thread(
                    session_pickup.delete_prefix, f"{session_pickup.COPIES_PREFIX}/{row['copyId']}/"
                )
                if row.get("itemPk"):
                    extra.append({"pk": row["itemPk"], "sk": "META"})

        def delete_page():
            with table.batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key={"pk": row["pk"], "sk": row["sk"]})
                for key in extra:
                    batch.delete_item(Key=key)

        await store.run_in_thread(delete_page)
        count += len(rows)
        if not response.get("LastEvaluatedKey"):
            return count
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
