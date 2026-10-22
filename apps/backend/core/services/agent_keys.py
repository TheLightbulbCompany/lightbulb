"""Per-agent keys for the hosted MCP, for callers with no browser to approve an OAuth sign-in
(scripts, CI, servers). Any MCP client that can do OAuth signs in with it instead: Clerk serves both
client ID metadata documents and dynamic client registration (on since 2026-10-22). A key acts
with its person's authority, in one workspace.

Rows live in the messaging table (no new infra), two per key:
    AGENTKEY#<sha256>   META                    the lookup the verifier reads
    OWNER#<owner>       AGENTKEY#<user>#<keyId> the person's list row; the owner wipe removes it
Only the SHA-256 is stored: keys are 256-bit random, so a plain hash is enough (no salt/KDF).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
import uuid

from botocore.exceptions import ClientError

from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import messaging_repo as repo

logger = logging.getLogger(__name__)

PREFIX = "lbk_"
MAX_KEYS_PER_PERSON = 10
DEFAULT_EXPIRY_DAYS = 90


class AgentKeyError(Exception):
    """A refusal the router maps to HTTP 400."""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _lookup_pk(key_hash: str) -> str:
    return f"AGENTKEY#{key_hash}"


def _count_sk(user_id: str) -> str:
    return f"AGENTKEYCOUNT#{user_id}"


def _list_sk(user_id: str, key_id: str) -> str:
    return f"AGENTKEY#{user_id}#{key_id}"


async def _rows(owner_id: str, user_id: str) -> list[dict]:
    return await repo._query(repo.owner_pk(owner_id), begins_with=f"AGENTKEY#{user_id}#")


async def list_keys(owner_id: str, user_id: str) -> list[dict]:
    rows = sorted(await _rows(owner_id, user_id), key=lambda r: r["createdAt"])
    return [{k: r.get(k) for k in ("keyId", "name", "createdAt", "expiresAt")} for r in rows]


async def create_key(owner_id: str, user_id: str, name: str, expires_in_days: int | None) -> dict:
    name = " ".join(name.split())
    if not name or len(name) > 60:
        raise AgentKeyError("Name the agent in 1-60 characters.")
    key = PREFIX + secrets.token_urlsafe(32)
    key_hash = _hash(key)
    key_id = uuid.uuid4().hex[:12]
    expires_at = int(time.time()) + expires_in_days * 86400 if expires_in_days else None
    row = {"keyId": key_id, "name": name, "createdAt": utc_now_iso(), "expiresAt": expires_at}
    table = repo._table().name
    # One transaction: the counter's condition is the cap, so two racing creates cannot both pass.
    try:
        await repo._transact(
            [
                {
                    "Update": {
                        "TableName": table,
                        "Key": {"pk": repo.owner_pk(owner_id), "sk": _count_sk(user_id)},
                        "UpdateExpression": "ADD n :one",
                        "ConditionExpression": "attribute_not_exists(n) OR n < :max",
                        "ExpressionAttributeValues": {":one": 1, ":max": MAX_KEYS_PER_PERSON},
                    }
                },
                {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            "pk": _lookup_pk(key_hash),
                            "sk": "META",
                            "ownerId": owner_id,
                            "userId": user_id,
                            **row,
                        },
                    }
                },
                {
                    "Put": {
                        "TableName": table,
                        "Item": {
                            "pk": repo.owner_pk(owner_id),
                            "sk": _list_sk(user_id, key_id),
                            "keyHash": key_hash,
                            **row,
                        },
                    }
                },
            ]
        )
    except ClientError as exc:
        if not repo._is_cancelled(exc):
            raise
        raise AgentKeyError(f"You can have at most {MAX_KEYS_PER_PERSON} agent keys. Revoke one first.") from exc
    logger.info("agent key created owner=%s user=%s keyId=%s", owner_id, user_id, key_id)
    return {**row, "key": key}  # the only time the key text exists outside the caller


async def revoke_key(owner_id: str, user_id: str, key_id: str) -> bool:
    """One transaction removes the lookup (which stops the next call), the list row and a count."""
    listed = await repo._get(repo.owner_pk(owner_id), _list_sk(user_id, key_id))
    if listed is None:
        return False
    table = repo._table().name
    try:
        await repo._transact(
            [
                {"Delete": {"TableName": table, "Key": {"pk": _lookup_pk(listed["keyHash"]), "sk": "META"}}},
                {
                    "Delete": {
                        "TableName": table,
                        "Key": {"pk": listed["pk"], "sk": listed["sk"]},
                        "ConditionExpression": "attribute_exists(sk)",
                    }
                },
                {
                    "Update": {
                        "TableName": table,
                        "Key": {"pk": repo.owner_pk(owner_id), "sk": _count_sk(user_id)},
                        "UpdateExpression": "ADD n :neg",
                        "ConditionExpression": "n > :zero",
                        "ExpressionAttributeValues": {":neg": -1, ":zero": 0},
                    }
                },
            ]
        )
    except ClientError as exc:
        if not repo._is_cancelled(exc):
            raise
        return False  # a concurrent revoke won
    logger.info("agent key revoked owner=%s user=%s keyId=%s", owner_id, user_id, key_id)
    return True


async def _delete_rows(rows: list[dict]) -> None:
    table = repo._table()
    for r in rows:
        await run_in_thread(table.delete_item, Key={"pk": _lookup_pk(r["keyHash"]), "sk": "META"})  # lookup first
        await run_in_thread(table.delete_item, Key={"pk": r["pk"], "sk": r["sk"]})


async def delete_for_member(owner_id: str, user_id: str) -> None:
    """Membership removal: a person who rejoins must not get their old keys back."""
    await _delete_rows(await _rows(owner_id, user_id))
    await run_in_thread(repo._table().delete_item, Key={"pk": repo.owner_pk(owner_id), "sk": _count_sk(user_id)})


async def delete_for_owner(owner_id: str) -> None:
    """Workspace/account wipe: the lookup rows live outside the OWNER# partition, so find them here."""
    await _delete_rows(await repo._query(repo.owner_pk(owner_id), begins_with="AGENTKEY#"))


async def resolve(key: str) -> dict | None:
    """The live lookup row for a key, or None (not ours, unknown, revoked or expired)."""
    if not key.startswith(PREFIX):
        return None
    row = await repo._get(_lookup_pk(_hash(key)), "META")
    if row is None or (row.get("expiresAt") and int(row["expiresAt"]) <= time.time()):
        return None
    return row
