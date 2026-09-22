"""Lightbulb-owned OpenRouter key lifecycle."""

import contextlib

import httpx
import boto3
import re
import time
import uuid

from core.dynamodb import run_in_thread
from core.config import settings
from core.repositories import billing_repo, openrouter_key_repo
from core.secrets_manager import create_or_update_secret


_SAFE_OWNER_ID = re.compile(r"^[A-Za-z0-9_-]+$")


async def create_completion_key(
    http: httpx.AsyncClient,
    *,
    management_key: str,
    owner_id: str,
    workspace_id: str,
    environment: str,
    limit_usd: float,
) -> dict[str, str]:
    """Create a capped key; caller must persist the one-time plaintext promptly."""
    if not management_key or not workspace_id or limit_usd <= 0:
        raise ValueError("OpenRouter management key, workspace, and positive limit are required")
    response = await http.post(
        "https://openrouter.ai/api/v1/keys",
        headers={"Authorization": f"Bearer {management_key}"},
        json={
            "name": f"lightbulb-{environment}-{owner_id}",
            "workspace_id": workspace_id,
            "limit": limit_usd,
            "limit_reset": "daily",
        },
    )
    response.raise_for_status()
    payload = response.json()
    key_hash = payload.get("data", {}).get("hash")
    plaintext = payload.get("key")
    if not key_hash or not plaintext:
        raise ValueError("OpenRouter did not return the key hash and one-time key")
    return {"hash": key_hash, "key": plaintext}


def limit_for_account(account: dict | None) -> float:
    """The owner key's OpenRouter daily $ limit: the valve for capped owners."""
    if (account or {}).get("subscription_status") == "trial_local":
        return settings.OPENROUTER_TRIAL_KEY_DAILY_LIMIT_USD
    return settings.OPENROUTER_BASE_KEY_DAILY_LIMIT_USD


async def update_key_limit(http, *, management_key, key_hash, limit_usd):
    if limit_usd <= 0:
        raise ValueError("OpenRouter key limit must be positive")
    response = await http.patch(
        f"https://openrouter.ai/api/v1/keys/{key_hash}",
        headers={"Authorization": f"Bearer {management_key}"},
        json={"limit": limit_usd, "limit_reset": "daily"},
    )
    response.raise_for_status()
    if response.json().get("data", {}).get("limit") != limit_usd:
        raise ValueError("OpenRouter did not confirm the key limit")


async def relimit_owner_key(owner_id: str, *, http=None, sm=None) -> bool:
    """Move the owner's active key onto its plan's daily limit. Idempotent:
    reads the key's current limit and PATCHes only on a difference. True when
    it changed the limit."""
    row = await openrouter_key_repo.get(owner_id)
    if not row or row.get("state") != "active":
        return False
    limit_usd = limit_for_account(await billing_repo.get_by_owner_id(owner_id, consistent_read=True))
    sm = sm or boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    management_name = f"lightbulb/{settings.ENVIRONMENT or 'dev'}/openrouter/management"
    management_key = (await run_in_thread(sm.get_secret_value, SecretId=management_name))["SecretString"]
    async with httpx.AsyncClient(timeout=10) if http is None else contextlib.nullcontext(http) as client:
        response = await client.get(
            f"https://openrouter.ai/api/v1/keys/{row['key_hash']}",
            headers={"Authorization": f"Bearer {management_key}"},
        )
        response.raise_for_status()
        if response.json().get("data", {}).get("limit") == limit_usd:
            return False
        await update_key_limit(client, management_key=management_key, key_hash=row["key_hash"], limit_usd=limit_usd)
    # A new limit is new headroom: lift today's gate-stamped cap marker.
    await billing_repo.clear_openrouter_capped_day(owner_id)
    return True


async def ensure_owner_key(owner_id, *, http, sm, environment, workspace_id, limit_usd, now_sec):
    if not _SAFE_OWNER_ID.fullmatch(owner_id):
        raise ValueError("Invalid owner ID for OpenRouter key provisioning")
    row = await openrouter_key_repo.get(owner_id)
    if row and row.get("state") == "active":
        if row.get("workspace_id") != workspace_id:
            raise RuntimeError("OpenRouter owner key belongs to a different workspace")
        await run_in_thread(sm.describe_secret, SecretId=row["secret_name"])
        return row["key_hash"]
    if row and row.get("state") == "retiring":
        if row.get("workspace_id") != workspace_id:
            raise RuntimeError("OpenRouter owner key belongs to a different workspace")
        await run_in_thread(sm.describe_secret, SecretId=row["secret_name"])
        if await openrouter_key_repo.cancel_retirement(owner_id, row["key_hash"]):
            return row["key_hash"]
        raise RuntimeError("OpenRouter owner key retirement is in progress")
    claim_id = str(uuid.uuid4())
    recovering = bool(row)
    if row and row.get("state") == "disabled":
        claimed = await openrouter_key_repo.claim_disabled(owner_id, claim_id, now_sec=now_sec)
    elif recovering:
        claimed = await openrouter_key_repo.reclaim_stale(owner_id, claim_id, now_sec=now_sec, ttl_sec=300)
    else:
        claimed = await openrouter_key_repo.claim_new(owner_id, claim_id, now_sec=now_sec)
    if not claimed:
        raise RuntimeError("OpenRouter key provisioning is already in progress")
    created = None
    management_key = None
    needs_reconciliation = recovering
    try:
        management_name = f"lightbulb/{environment}/openrouter/management"
        management_key = (await run_in_thread(sm.get_secret_value, SecretId=management_name))["SecretString"]
        if recovering:
            for orphan_hash in await list_owner_keys(
                http,
                management_key=management_key,
                owner_id=owner_id,
                environment=environment,
                workspace_id=workspace_id,
            ):
                await disable_completion_key(http, management_key=management_key, key_hash=orphan_hash)
        needs_reconciliation = True  # A timeout can mean OpenRouter created a key anyway.
        created = await create_completion_key(
            http,
            management_key=management_key,
            owner_id=owner_id,
            workspace_id=workspace_id,
            environment=environment,
            limit_usd=limit_usd,
        )
        # A losing stale claim must never overwrite the winner's plaintext.
        secret_name = f"lightbulb/{environment}/openrouter/owners/{owner_id}/{created['hash']}"
        await run_in_thread(create_or_update_secret, sm, secret_name, created["key"])
        if not await openrouter_key_repo.activate(owner_id, claim_id, created["hash"], secret_name, workspace_id):
            raise RuntimeError("OpenRouter owner key claim changed during provisioning")
        return created["hash"]
    except Exception:
        if created and management_key:
            # Keep the pending claim if disabling fails, so recovery can find the orphan.
            await disable_completion_key(http, management_key=management_key, key_hash=created["hash"])
            needs_reconciliation = False
        if not needs_reconciliation:
            await openrouter_key_repo.drop_pending(owner_id, claim_id)
        raise


async def disable_completion_key(http, *, management_key, key_hash):
    response = await http.patch(
        f"https://openrouter.ai/api/v1/keys/{key_hash}",
        headers={"Authorization": f"Bearer {management_key}"},
        json={"disabled": True},
    )
    response.raise_for_status()
    if response.json().get("data", {}).get("disabled") is not True:
        raise ValueError("OpenRouter did not confirm key disable")


async def list_owner_keys(http, *, management_key, owner_id, environment, workspace_id):
    name = f"lightbulb-{environment}-{owner_id}"
    hashes = []
    offset = 0
    while True:
        response = await http.get(
            "https://openrouter.ai/api/v1/keys",
            headers={"Authorization": f"Bearer {management_key}"},
            params={"offset": offset, "workspace_id": workspace_id},
        )
        response.raise_for_status()
        page = response.json().get("data")
        if not isinstance(page, list):
            raise ValueError("OpenRouter key list has no data array")
        hashes.extend(key["hash"] for key in page if key.get("name") == name and not key.get("disabled"))
        if len(page) < 100:
            return hashes
        offset += len(page)


async def ensure_owner_key_for_settings(owner_id: str) -> str:
    if not settings.OPENROUTER_WORKSPACE_ID:
        raise RuntimeError("OpenRouter workspace is not configured")
    limit_usd = limit_for_account(await billing_repo.get_by_owner_id(owner_id, consistent_read=True))
    if limit_usd <= 0:
        raise RuntimeError("OpenRouter owner key daily limit is not configured")
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    async with httpx.AsyncClient(timeout=10) as http:
        return await ensure_owner_key(
            owner_id,
            http=http,
            sm=sm,
            environment=settings.ENVIRONMENT or "dev",
            workspace_id=settings.OPENROUTER_WORKSPACE_ID,
            limit_usd=limit_usd,
            now_sec=int(time.time()),
        )
