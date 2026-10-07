"""Local harness registry in the messaging table; directory rows never grant access.

LOCALUSER#<scope>#<human> / DEVICE#<mac> and SESSION#<id>
LOCALSESSION#<scope>#<id> / META, EVENT#<20-digit seq>, COMMAND#<requestId>
No TTL: command delivery deadlines do not delete handoff history or receipts.
"""

from __future__ import annotations

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


def pairing_condition(owner_id: str, user_id: str, device_id: str, cli_device_id: str | None) -> dict:
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


async def put_enrollment(
    owner_id: str,
    user_id: str,
    device_id: str,
    cli_device_id: str | None,
    enabled: bool,
    expected_revision: str | None = None,
    *,
    enable_revision: str | None = None,
    previous_revision: str | None = None,
    has_predecessor: bool = False,
) -> dict:
    row = {
        "pk": user_pk(owner_id, user_id),
        "sk": f"DEVICE#{device_id}",
        "ownerId": owner_id,
        "humanUserId": user_id,
        "deviceId": device_id,
        "cliDeviceId": cli_device_id,
        "enabled": enabled,
        "revision": enable_revision or expected_revision or str(uuid.uuid4()),
    }
    put = {"TableName": store._table().name, "Item": row}
    if expected_revision is not None:
        put.update(
            ConditionExpression="revision = :expected", ExpressionAttributeValues={":expected": expected_revision}
        )
    if has_predecessor:
        # Cancellation may win before the first enable arrives. Writing the
        # attempted revision disabled fences both that request and all replays.
        values = {":no": False} if previous_revision is not None else {}
        predecessor = "attribute_not_exists(pk)"
        if previous_revision is not None:
            predecessor = "(revision = :previous AND enabled = :no)"
            values[":previous"] = previous_revision
        condition = predecessor
        if not enabled:
            condition = f"(revision = :expected OR {predecessor})"
            values[":expected"] = expected_revision
        put["ConditionExpression"] = condition
        if values:
            put["ExpressionAttributeValues"] = values
    await store._transact(
        [
            pairing_condition(owner_id, user_id, device_id, cli_device_id),
            {"Put": put},
        ]
    )
    return row


async def put_session(row: dict, previous: dict | None, enrollment: dict, *, extra: Sequence[dict] = ()) -> None:
    """CAS preserves concurrent command ordering; canonical and directory land together."""
    put = {"TableName": store._table().name, "Item": row, "ConditionExpression": "attribute_not_exists(sk)"}
    if previous:
        put.update(
            ConditionExpression="revision = :revision", ExpressionAttributeValues={":revision": previous["revision"]}
        )
    projection = {key: row[key] for key in ("ownerId", "humanUserId", "deviceId", "sessionId")}
    projection.update(pk=user_pk(row["ownerId"], row["humanUserId"]), sk=f"SESSION#{row['sessionId']}")
    await store._transact(
        [
            pairing_condition(row["ownerId"], row["humanUserId"], row["deviceId"], None),
            enrollment_condition(enrollment),
            {"Put": put},
            {"Put": {"TableName": store._table().name, "Item": projection}},
        ]
        + list(extra)
    )


def room_conditions(owner: str, human: str, receipt: dict) -> list[dict]:
    table, room = store._table().name, receipt["roomId"]
    actor = ("agent", receipt["actor"]["id"]) if "actor" in receipt else ("human", receipt["actorUserId"])
    return [
        {
            "ConditionCheck": {
                "TableName": table,
                "Key": {"pk": store.room_pk(room), "sk": "META"},
                "ConditionExpression": "ownerId = :owner",
                "ExpressionAttributeValues": {":owner": owner},
            }
        }
    ] + [
        {
            "ConditionCheck": {
                "TableName": table,
                "Key": {"pk": store.room_pk(room), "sk": store.member_sk(kind, user)},
                "ConditionExpression": "attribute_exists(sk)",
            }
        }
        for kind, user in sorted({("human", human), actor})
    ]


async def list_commands(owner: str, sid: str) -> list[dict]:
    # ponytail: per-session receipt history scan; add an ordered pending index if measured volume warrants it.
    return sorted(await store._query(session_pk(owner, sid), begins_with="COMMAND#"), key=lambda row: row["commandSeq"])


async def replace_command(previous: dict, updated: dict, *, checks: Sequence[dict] = ()) -> None:
    put = {
        "TableName": store._table().name,
        "Item": updated,
        "ConditionExpression": "attribute_not_exists(revision) AND #state = :state",
        "ExpressionAttributeNames": {"#state": "state"},
        "ExpressionAttributeValues": {":state": previous["state"]},
    }
    if previous.get("revision"):
        put["ConditionExpression"] = "#state = :state AND revision = :revision"
        put["ExpressionAttributeValues"][":revision"] = previous["revision"]
    await store._transact([*checks, {"Put": put}])


async def admit_command(row: dict, enrollment: dict, receipt: dict) -> None:
    owner, human, device = row["ownerId"], row["humanUserId"], row["deviceId"]
    table = store._table().name
    await store._transact(
        room_conditions(owner, human, receipt)
        + [
            pairing_condition(owner, human, device, None),
            enrollment_condition(enrollment),
            {
                "Update": {
                    "TableName": table,
                    "Key": {"pk": row["pk"], "sk": "META"},
                    "UpdateExpression": "SET commandSeq = :next, revision = :newRevision",
                    "ConditionExpression": "revision = :revision AND generation = :generation AND onlineUntil > :now AND attribute_not_exists(endedAt)",
                    "ExpressionAttributeValues": {
                        ":next": receipt["commandSeq"],
                        ":revision": row["revision"],
                        ":newRevision": str(uuid.uuid4()),
                        ":generation": receipt["expectedGeneration"],
                        ":now": receipt["createdAt"],
                    },
                }
            },
            {"Put": {"TableName": table, "Item": receipt, "ConditionExpression": "attribute_not_exists(sk)"}},
        ]
    )


async def delete_owner(owner_id: str) -> int:
    """Called by the account wipe after pairing authority has been removed."""
    from boto3.dynamodb.conditions import Attr

    table = store._table()
    # ponytail: deletion-only table scan; add a catalog if measured wipe volume warrants it.
    kwargs = {
        "FilterExpression": Attr("pk").begins_with(f"LOCALUSER#{owner_id}#")
        | Attr("pk").begins_with(f"LOCALSESSION#{owner_id}#"),
        "ProjectionExpression": "pk, sk",
        "ConsistentRead": True,
    }
    count = 0
    while True:
        response = await store.run_in_thread(table.scan, **kwargs)
        rows = response.get("Items", [])

        def delete_page():
            with table.batch_writer() as batch:
                for row in rows:
                    batch.delete_item(Key=row)

        await store.run_in_thread(delete_page)
        count += len(rows)
        if not response.get("LastEvaluatedKey"):
            return count
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
