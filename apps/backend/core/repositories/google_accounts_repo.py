"""Per-agent Google account record store.

Two record families, both stored on the existing ``integrations`` table
(owner_id PK, provider SK) — no new table, no schema change. The SK is
namespaced so both families coexist under one owner partition:

  - ``google#account#<email>``  — inventory of a connected Google account
                                  (carries ``connected_at``).
  - ``google#assign#<agent_id>`` — which account an agent is bound to
                                   (carries ``email``).

A Google email address never contains ``#``, so splitting the SK on ``#``
recovers the email / agent_id cleanly — the prefix is unambiguous and the
final segment is the value verbatim.
"""

from __future__ import annotations

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso

_ACCOUNT_PREFIX = "google#account#"
_ASSIGN_PREFIX = "google#assign#"


def _table():
    return get_table("integrations")  # short name; table prefix auto-applied


# ---------------------------------------------------------------------------
# Account inventory
# ---------------------------------------------------------------------------


async def add_account(owner_id: str, email: str) -> None:
    """Record that ``owner_id`` has connected the Google account ``email``.

    Idempotent: re-adding the same email updates the existing row rather than
    creating a duplicate, since the email is part of the SK.

    ``connected_at`` is set only on first add and PRESERVED on re-add via
    ``if_not_exists``. The default account is the oldest (smallest
    ``connected_at``), so bumping the timestamp on a re-authorize would reorder
    accounts and silently flip the owner's default — keeping the original
    timestamp makes the oldest-default stable across reconnects.
    """
    await run_in_thread(
        _table().update_item,
        Key={"owner_id": owner_id, "provider": f"{_ACCOUNT_PREFIX}{email}"},
        UpdateExpression="SET connected_at = if_not_exists(connected_at, :now)",
        ExpressionAttributeValues={":now": utc_now_iso()},
    )


async def remove_account(owner_id: str, email: str) -> None:
    await run_in_thread(
        _table().delete_item,
        Key={"owner_id": owner_id, "provider": f"{_ACCOUNT_PREFIX}{email}"},
    )


async def list_accounts(owner_id: str) -> list[dict]:
    """Owner's connected Google accounts, oldest first.

    Sorted by ``connected_at`` so the head of the list is the default
    account used when an agent has no explicit assignment.
    """
    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    accounts = [
        {
            "email": item["provider"][len(_ACCOUNT_PREFIX) :],
            "connected_at": item.get("connected_at"),
        }
        for item in response.get("Items", [])
        if item["provider"].startswith(_ACCOUNT_PREFIX)
    ]
    accounts.sort(key=lambda a: a["connected_at"] or "")
    return accounts


# ---------------------------------------------------------------------------
# Per-agent assignments
# ---------------------------------------------------------------------------


async def get_assignment(owner_id: str, agent_id: str) -> str | None:
    """The agent's EXPLICITLY assigned Google account, or None.

    No default fallback: an unassigned agent returns None even when the owner
    has connected accounts. The IDENTITY.md seeder reflects only an explicit
    assignment, so falling back to a default here would write a block that no
    longer gets cleaned up when that default account is later disconnected.
    """
    response = await run_in_thread(
        _table().get_item,
        Key={"owner_id": owner_id, "provider": f"{_ASSIGN_PREFIX}{agent_id}"},
    )
    item = response.get("Item")
    return item["email"] if item else None


async def remove_assignment(owner_id: str, agent_id: str) -> None:
    await run_in_thread(
        _table().delete_item,
        Key={"owner_id": owner_id, "provider": f"{_ASSIGN_PREFIX}{agent_id}"},
    )


async def list_assignments(owner_id: str) -> dict[str, str]:
    """Map of agent_id → assigned account email for ``owner_id``."""
    response = await run_in_thread(
        _table().query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return {
        item["provider"][len(_ASSIGN_PREFIX) :]: item["email"]
        for item in response.get("Items", [])
        if item["provider"].startswith(_ASSIGN_PREFIX)
    }
