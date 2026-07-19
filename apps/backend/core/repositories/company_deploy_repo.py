"""Company-deploy job repository -- DynamoDB operations for the marketplace
company-deploy job ledger (``marketplace-company-deploys``).

Like the other marketplace repos, this table is addressed by a full-name
setting (``MARKETPLACE_COMPANY_DEPLOYS_TABLE``), so the accessor builds
``boto3.resource("dynamodb")`` directly instead of going through
``core.dynamodb.get_table``.

A job row is the ledger + polling surface for one company deploy:

    {job_id, owner_id, listing_id, listing_version, company_slug,
     status: "running"|"failed"|"completed"|"rolled_back",
     agents: [{slug, paperclip_id, state}],
     routine_ids: [str], error: str|None, created_at, updated_at}

Each agent entry is seeded ``pending`` at create time and advanced by
``record_agent`` as it materializes. ``record_agent`` read-modify-writes the
whole ``agents`` list: the list is small (one entry per company agent), so a
targeted list-index update buys nothing and the RMW keeps the write simple.
"""

import uuid

import boto3

from core.config import settings
from core.dynamodb import run_in_thread


def _table():
    return boto3.resource("dynamodb").Table(settings.MARKETPLACE_COMPANY_DEPLOYS_TABLE)


async def create_job(
    *,
    owner_id: str,
    listing_id: str,
    listing_version: int,
    company_slug: str,
    agent_slugs: list[str],
    now_iso: str,
) -> str:
    """Mint a job_id and write a ``running`` row with the agent list seeded
    from the plan slugs in ``pending`` state. Returns the job_id."""
    job_id = f"cdpl_{uuid.uuid4().hex}"
    table = _table()
    await run_in_thread(
        table.put_item,
        Item={
            "job_id": job_id,
            "owner_id": owner_id,
            "listing_id": listing_id,
            "listing_version": listing_version,
            "company_slug": company_slug,
            "status": "running",
            "agents": [{"slug": slug, "paperclip_id": None, "state": "pending"} for slug in agent_slugs],
            "routine_ids": [],
            "error": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        },
    )
    return job_id


async def get_job(job_id: str) -> dict | None:
    """Look up one job row by job_id, or None."""
    table = _table()
    resp = await run_in_thread(table.get_item, Key={"job_id": job_id})
    return resp.get("Item")


async def record_agent(
    *,
    job_id: str,
    slug: str,
    paperclip_id: str | None,
    state: str,
    now_iso: str,
) -> None:
    """Update the matching agent entry's paperclip_id + state and bump
    updated_at (read-modify-write the whole agents list). No-op if the job
    row is gone."""
    table = _table()
    resp = await run_in_thread(table.get_item, Key={"job_id": job_id})
    item = resp.get("Item")
    if item is None:
        return
    agents = item.get("agents", [])
    for agent in agents:
        if agent.get("slug") == slug:
            agent["paperclip_id"] = paperclip_id
            agent["state"] = state
            break
    await run_in_thread(
        table.update_item,
        Key={"job_id": job_id},
        UpdateExpression="SET agents = :agents, updated_at = :now",
        ExpressionAttributeValues={":agents": agents, ":now": now_iso},
    )


async def record_routine_ids(
    *,
    job_id: str,
    routine_ids: list[str],
    now_iso: str,
) -> None:
    """Overwrite the job's ``routine_ids`` with the full created-routine list and
    bump updated_at.

    Called once after the routines are created (before the terminal
    ``completed`` status) so the polling surface / RoutinesEnableCard sees the
    ids that shipped. ``routine_ids`` is not a DynamoDB reserved word, so it
    needs no alias. ``attribute_exists(job_id)`` guards a missing row so a stray
    call never conjures a phantom job — a single write, mirroring ``set_status``."""
    table = _table()
    await run_in_thread(
        table.update_item,
        Key={"job_id": job_id},
        UpdateExpression="SET routine_ids = :ids, updated_at = :now",
        ConditionExpression="attribute_exists(job_id)",
        ExpressionAttributeValues={":ids": routine_ids, ":now": now_iso},
    )


async def set_status(
    *,
    job_id: str,
    status: str,
    now_iso: str,
    error: str | None = None,
) -> None:
    """Flip the job status (+ optional error) and bump updated_at.

    ``status`` is a DynamoDB reserved word, so it's routed through an
    ``#s`` alias; ``error`` is aliased alongside for symmetry."""
    table = _table()
    await run_in_thread(
        table.update_item,
        Key={"job_id": job_id},
        UpdateExpression="SET #s = :status, #e = :error, updated_at = :now",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":status": status, ":error": error, ":now": now_iso},
    )
