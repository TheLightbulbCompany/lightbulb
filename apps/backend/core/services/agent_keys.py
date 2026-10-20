"""Per-agent keys for the hosted MCP: a fallback for agents that cannot do OAuth (Clerk has no
dynamic client registration). A key acts with its person's authority, in one workspace.

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
    if len(await _rows(owner_id, user_id)) >= MAX_KEYS_PER_PERSON:
        raise AgentKeyError(f"You can have at most {MAX_KEYS_PER_PERSON} agent keys. Revoke one first.")
    key = PREFIX + secrets.token_urlsafe(32)
    key_hash = _hash(key)
    key_id = uuid.uuid4().hex[:12]
    expires_at = int(time.time()) + expires_in_days * 86400 if expires_in_days else None
    row = {"keyId": key_id, "name": name, "createdAt": utc_now_iso(), "expiresAt": expires_at}
    table = repo._table()
    await run_in_thread(
        table.put_item,
        Item={"pk": _lookup_pk(key_hash), "sk": "META", "ownerId": owner_id, "userId": user_id, **row},
    )
    await run_in_thread(
        table.put_item,
        Item={"pk": repo.owner_pk(owner_id), "sk": _list_sk(user_id, key_id), "keyHash": key_hash, **row},
    )
    logger.info("agent key created owner=%s user=%s keyId=%s", owner_id, user_id, key_id)
    return {**row, "key": key}  # the only time the key text exists outside the caller


async def revoke_key(owner_id: str, user_id: str, key_id: str) -> bool:
    """Delete the lookup row first: that is what stops the next call."""
    listed = await repo._get(repo.owner_pk(owner_id), _list_sk(user_id, key_id))
    if listed is None:
        return False
    table = repo._table()
    await run_in_thread(table.delete_item, Key={"pk": _lookup_pk(listed["keyHash"]), "sk": "META"})
    await run_in_thread(table.delete_item, Key={"pk": listed["pk"], "sk": listed["sk"]})
    logger.info("agent key revoked owner=%s user=%s keyId=%s", owner_id, user_id, key_id)
    return True


async def resolve(key: str) -> dict | None:
    """The live lookup row for a key, or None (not ours, unknown, revoked or expired)."""
    if not key.startswith(PREFIX):
        return None
    row = await repo._get(_lookup_pk(_hash(key)), "META")
    if row is None or (row.get("expiresAt") and int(row["expiresAt"]) <= time.time()):
        return None
    return row
