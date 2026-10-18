"""The workspace's agent mailbox row: one per workspace, keyed by its team
address ``agents@{slug}.{domain}`` (inbound mail is routed by recipient); a
``by-owner`` GSI finds it, and both wipes delete through it.

A workspace with a ``public_address`` (``scripts/set_team_mail_identity.py``)
also has an ALIAS row keyed by it: ``{address: <public>, alias_of: <routing>,
owner_id}``. It holds no settings; ``resolve`` follows it, the wipes delete it
with the owner's other rows, and ``agent_email.strictest`` skips it.

Prefix-convention table (``core.dynamodb.get_table``), matching
``channel_link_repo``.
"""

import logging
from typing import Literal

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.repositories import company_slug_repo

logger = logging.getLogger(__name__)


def _get_table():
    return get_table("agent-email-addresses")


def normalize(value: str) -> str:
    """Addresses and sender emails are matched case-insensitively."""
    return value.strip().lower()


async def put(
    *,
    address: str,
    owner_id: str,
    verified_senders: list[str],
    send_to_anyone: bool = True,
    outbound_disabled: str | None = None,
) -> dict | None:
    """Create the workspace's row — a CREATE, never an overwrite: conditional on
    ``attribute_not_exists(address)``, so a racing create can never reset the
    owner's settings. Returns the item, or None when a row already exists.

    New rows are OPEN (``send_to_anyone=True``, owner ruling 2026-09-24): any
    authenticated sender may wake the agents and they may email anyone. The
    owner turns it off with ``update_settings``. The two keyword fences exist
    for a row seeded from stricter legacy rows (``ensure_team_address``).
    """
    item = {
        "address": normalize(address),
        "owner_id": owner_id,
        "verified_senders": [normalize(s) for s in verified_senders],
        "send_to_anyone": send_to_anyone,
        "status": "active",
        "created_at": utc_now_iso(),
    }
    if outbound_disabled:
        item["outbound_disabled"] = outbound_disabled
    try:
        await run_in_thread(_get_table().put_item, Item=item, ConditionExpression="attribute_not_exists(address)")
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def get_by_address(address: str) -> dict | None:
    """Strongly consistent: ``ensure_team_address`` reads it before and after a
    create, and an eventual miss right after the write would report a failed mint."""
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"address": normalize(address)}, ConsistentRead=True)
    return response.get("Item")


async def resolve(address: str) -> dict | None:
    """The mailbox row for ``address``, following an alias row's ``alias_of`` once."""
    row = await get_by_address(address)
    if row and row.get("alias_of"):
        return await get_by_address(row["alias_of"])
    return row


async def is_team_address(address: str) -> tuple[str, str] | None:
    """``(owner_id, slug)`` when ``address`` is ``agents@{slug}.{domain}`` for a
    reserved company slug — whether or not its row exists yet."""
    return await owner_for_host(address) if normalize(address).partition("@")[0] == "agents" else None


async def owner_for_host(address: str) -> tuple[str, str] | None:
    """``(owner_id, slug)`` when ``address``'s host is ``{slug}.{domain}`` for a reserved
    company slug, whatever the local part — so a retired ``friday@{slug}…`` still resolves."""
    host = normalize(address).partition("@")[2]
    domain = (settings.AGENT_EMAIL_DOMAIN or "").strip().lower()
    if not domain:
        return None
    slug = host.split(".", 1)[0]
    if host != f"{slug}.{domain}":
        return None
    row = await company_slug_repo.get_by_slug(slug)
    if not row or str(row.get("owner_id", "")).startswith(company_slug_repo.PENDING_PREFIX):
        return None
    return row["owner_id"], slug


async def query_by_owner(owner_id: str) -> list[dict]:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-owner",
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])


async def delete_all_for_owner(owner_id: str) -> list[str]:
    """Delete every agent email address owned by this owner. Returns them.

    The wipe used to skip this, so a deleted owner's addresses stayed in the
    table and inbound SES mail to them routed at an owner that no longer
    exists. Reads through the by-owner GSI (eventually consistent — a write
    from the same second could be missed, which is acceptable for a teardown
    that runs after the owner's surfaces are already gone).
    """
    rows = await query_by_owner(owner_id)
    table = _get_table()
    addresses: list[str] = []
    for row in rows:
        address = row.get("address")
        if not address:
            continue
        await run_in_thread(table.delete_item, Key={"address": address})
        addresses.append(address)
    return addresses


_UNSET = object()


async def update_settings(
    address: str,
    *,
    verified_senders: list[str] | None = None,
    send_to_anyone: bool | None = None,
    footer_enabled: bool | None = None,
    outbound_disabled: str | None | object = _UNSET,
) -> dict | None:
    """SET the supplied owner-editable fields on an existing row; None for
    ``outbound_disabled`` REMOVEs it. Returns the updated item, or None when
    the row does not exist (conditional on ``attribute_exists(address)``)."""
    sets: list[str] = []
    removes: list[str] = []
    values: dict = {}
    if verified_senders is not None:
        sets.append("verified_senders = :vs")
        values[":vs"] = [normalize(s) for s in verified_senders]
    if send_to_anyone is not None:
        sets.append("send_to_anyone = :sta")
        values[":sta"] = bool(send_to_anyone)
    if footer_enabled is not None:
        sets.append("footer_enabled = :fe")
        values[":fe"] = bool(footer_enabled)
    if outbound_disabled is not _UNSET:
        if outbound_disabled is None:
            removes.append("outbound_disabled")
        else:
            sets.append("outbound_disabled = :od")
            values[":od"] = str(outbound_disabled)
    if not sets and not removes:
        return await get_by_address(address)
    expr = ""
    if sets:
        expr += "SET " + ", ".join(sets)
    if removes:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(removes)
    kwargs = {
        "Key": {"address": normalize(address)},
        "UpdateExpression": expr,
        "ConditionExpression": "attribute_exists(address)",
        "ReturnValues": "ALL_NEW",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        resp = await run_in_thread(_get_table().update_item, **kwargs)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return resp.get("Attributes")


async def put_text_line(
    *,
    number: str,
    owner_id: str,
    agent_id: str,
    photon_resource_id: str | None = None,
    photon_user_id: str | None = None,
) -> dict | None:
    """Create the workspace's texting-line row, keyed by its E.164 number. A CREATE, never an
    overwrite; None when the number already has a row."""
    item = {
        "address": number,
        "owner_id": owner_id,
        "agent_id": agent_id,
        "kind": "text",
        "status": "active",
        "created_at": utc_now_iso(),
        "human_threads": {},
        "member_phones": {},
    }
    if photon_resource_id:
        item["photon_resource_id"] = photon_resource_id
    if photon_user_id:
        item["photon_user_id"] = photon_user_id
    try:
        await run_in_thread(_get_table().put_item, Item=item, ConditionExpression="attribute_not_exists(address)")
        return item
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise


async def put_pending_line(*, owner_id: str, agent_id: str, operation_id: str) -> bool:
    """The in-flight marker for a line Photon is still creating; one per owner. False when one exists."""
    item = {
        "address": f"pending:{owner_id}",
        "owner_id": owner_id,
        "agent_id": agent_id,
        "kind": "text_pending",
        "operation_id": operation_id,
        "created_at": utc_now_iso(),
    }
    try:
        await run_in_thread(_get_table().put_item, Item=item, ConditionExpression="attribute_not_exists(address)")
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def pending_line_for_owner(owner_id: str) -> dict | None:
    row = await get_by_address(f"pending:{owner_id}")
    return row if row and row.get("kind") == "text_pending" else None


async def delete_pending_line(owner_id: str, *, operation_id: str) -> bool:
    """True when this call removed the row; False when it was already gone or now names another operation
    (another reader won, or a newer request replaced it)."""
    try:
        await run_in_thread(
            _get_table().delete_item,
            Key={"address": f"pending:{owner_id}"},
            ConditionExpression="kind = :pending AND operation_id = :op",
            ExpressionAttributeValues={":pending": "text_pending", ":op": operation_id},
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


async def delete_text_line(number: str) -> None:
    """Delete a texting-line row; the condition means it can never delete a mail address row."""
    try:
        await run_in_thread(
            _get_table().delete_item,
            Key={"address": number},
            ConditionExpression="kind = :text",
            ExpressionAttributeValues={":text": "text"},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise


async def text_line_for_owner(owner_id: str) -> dict | None:
    return next((r for r in await query_by_owner(owner_id) if r.get("kind") == "text"), None)


async def text_line_by_photon_user(user_id: str) -> dict | None:
    # ponytail: rare path (receivedBy.handle missing); a GSI if it is ever hot
    kwargs: dict = {"FilterExpression": Attr("photon_user_id").eq(user_id) & Attr("kind").eq("text")}
    while True:
        resp = await run_in_thread(_get_table().scan, **kwargs)
        if resp.get("Items"):
            return resp["Items"][0]
        if not resp.get("LastEvaluatedKey"):
            return None
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


async def set_business_phone(number: str, phone: str) -> None:
    await run_in_thread(
        _get_table().update_item,
        Key={"address": number},
        UpdateExpression="SET business_phone = :p",
        ConditionExpression="attribute_exists(address)",
        ExpressionAttributeValues={":p": phone},
    )


async def set_thread_mode(number: str, customer: str, mode: Literal["human", "agent"]) -> None:
    """``human`` marks a customer's thread as taken over by a person; ``agent`` hands it back.
    ``customer`` is a phone number or an email address, so it goes in as a name placeholder."""
    kwargs: dict = {
        "Key": {"address": number},
        "ConditionExpression": "attribute_exists(address)",
        "ExpressionAttributeNames": {"#c": customer},
    }
    if mode == "human":
        kwargs["UpdateExpression"] = "SET human_threads.#c = :iso"
        kwargs["ExpressionAttributeValues"] = {":iso": utc_now_iso()}
    else:
        kwargs["UpdateExpression"] = "REMOVE human_threads.#c"
    await run_in_thread(_get_table().update_item, **kwargs)


async def set_member_phone(number: str, phone: str, user_id: str | None) -> None:
    """Register (``user_id``) or remove (``None``) a member's phone on the line row, so their texts
    are posted to the main room as them. The ``member_phones`` map is created with the line."""
    kwargs: dict = {
        "Key": {"address": number},
        "ConditionExpression": "attribute_exists(address)",
        "ExpressionAttributeNames": {"#p": phone},
    }
    if user_id:
        kwargs["UpdateExpression"] = "SET member_phones.#p = :uid"
        kwargs["ExpressionAttributeValues"] = {":uid": user_id}
    else:
        kwargs["UpdateExpression"] = "REMOVE member_phones.#p"
    await run_in_thread(_get_table().update_item, **kwargs)


async def add_meeting(address: str, meeting_id: str) -> None:
    """Record a meeting (its room id) on the workspace's row, for the team inbox. A DynamoDB ``ADD``
    to a string set, so a replay is a no-op; ids are never removed. Best effort: the team-mail path
    must not fail over a listing, so a failure is logged and swallowed."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"address": normalize(address)},
            UpdateExpression="ADD meetings :m",
            ConditionExpression="attribute_exists(address)",
            ExpressionAttributeValues={":m": {meeting_id}},
        )
    except Exception:  # noqa: BLE001 -- see docstring
        logger.warning("agent_email_repo: add_meeting failed for %s", meeting_id, exc_info=True)
