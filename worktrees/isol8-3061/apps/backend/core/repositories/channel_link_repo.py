"""Channel link repository — DynamoDB operations for the channel-links table.

Stores per-member identity links to per-agent channel bots. Primary key is
(owner_id, sk="provider#agent_id#peer_id"). The by-member GSI supports
querying all links for one Clerk member across all their orgs.
"""

from boto3.dynamodb.conditions import Key

from core.dynamodb import get_table, run_in_thread, utc_now_iso


def _get_table():
    return get_table("channel-links")


def _sk(provider: str, agent_id: str, peer_id: str) -> str:
    return f"{provider}#{agent_id}#{peer_id}"


def _owner_provider_agent(owner_id: str, provider: str, agent_id: str) -> str:
    return f"{owner_id}#{provider}#{agent_id}"


async def _batch_delete(table, items: list[dict]) -> None:
    """Delete the given items from a DynamoDB table using batch_writer.

    Runs the entire batch_writer lifetime (including flush on __exit__)
    inside a single run_in_thread call so boto3's BatchWriter — which
    is NOT thread-safe — stays on one worker thread. Called by the sweep
    helpers below.

    Chunks items into 25-row batches (DynamoDB BatchWriteItem max).
    """
    if not items:
        return

    def _do_batch(batch_items: list[dict]) -> None:
        with table.batch_writer() as w:
            for item in batch_items:
                w.delete_item(Key={"owner_id": item["owner_id"], "sk": item["sk"]})

    for i in range(0, len(items), 25):
        chunk = items[i : i + 25]
        await run_in_thread(_do_batch, chunk)


async def put(
    *,
    owner_id: str,
    provider: str,
    agent_id: str,
    peer_id: str,
    member_id: str,
    linked_via: str,
) -> dict:
    """Create or overwrite a channel link row.

    Returns the constructed item dict (with the client-side ``linked_at``
    timestamp) — does NOT re-fetch from DynamoDB, so callers should treat
    ``linked_at`` as the wall-clock time when ``put`` was called rather
    than the server-side write time.
    """
    item = {
        "owner_id": owner_id,
        "sk": _sk(provider, agent_id, peer_id),
        "provider": provider,
        "agent_id": agent_id,
        "peer_id": peer_id,
        "member_id": member_id,
        "linked_via": linked_via,
        "linked_at": utc_now_iso(),
        # Denormalized composite for the by-member GSI sort key
        "owner_provider_agent": _owner_provider_agent(owner_id, provider, agent_id),
    }
    table = _get_table()
    await run_in_thread(table.put_item, Item=item)
    return item


async def get_by_peer(
    *,
    owner_id: str,
    provider: str,
    agent_id: str,
    peer_id: str,
) -> dict | None:
    """Look up a single link row by its full primary key."""
    table = _get_table()
    response = await run_in_thread(
        table.get_item,
        Key={"owner_id": owner_id, "sk": _sk(provider, agent_id, peer_id)},
    )
    return response.get("Item")


async def query_by_member(member_id: str) -> list[dict]:
    """Return all link rows for a Clerk member across all orgs.

    Uses the by-member GSI. Does not paginate — acceptable because a
    member is realistically in at most a handful of orgs with a few bots
    each, so the result set fits in a single 1MB query page.
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="by-member",
        KeyConditionExpression=Key("member_id").eq(member_id),
    )
    return response.get("Items", [])


async def query_by_owner(owner_id: str) -> list[dict]:
    """Return all link rows for a container owner across all providers/agents/peers.

    Uses the main table's hash key directly. Note: does not paginate —
    acceptable because per-owner link counts are small (bounded by number
    of bots × number of members linked).
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
    )
    return response.get("Items", [])


async def delete(
    *,
    owner_id: str,
    provider: str,
    agent_id: str,
    peer_id: str,
) -> None:
    """Delete a single link row."""
    table = _get_table()
    await run_in_thread(
        table.delete_item,
        Key={"owner_id": owner_id, "sk": _sk(provider, agent_id, peer_id)},
    )


async def sweep_by_owner_provider_agent(
    *,
    owner_id: str,
    provider: str,
    agent_id: str,
) -> int:
    """Delete all link rows for one bot. Used by bot-delete cleanup.

    Queries the main table with a ``provider#agent_id#`` SK prefix and
    deletes all matches. Does NOT paginate — acceptable because a single
    bot's linked-member count is bounded by the org's member count, which
    fits in a single 1MB query page in practice. If a bot ever accumulates
    >~400 linked peers, items beyond the page boundary would be silently
    left behind; revisit pagination if that becomes a real concern.

    Returns the number of rows deleted.
    """
    table = _get_table()
    prefix = f"{provider}#{agent_id}#"
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("owner_id").eq(owner_id) & Key("sk").begins_with(prefix),
    )
    items = response.get("Items", [])
    await _batch_delete(table, items)
    return len(items)


async def sweep_by_owner(owner_id: str) -> int:
    """Delete all link rows for one container. Used by container-delete cleanup.

    Returns the number of rows deleted.
    """
    items = await query_by_owner(owner_id)
    await _batch_delete(_get_table(), items)
    return len(items)


async def delete_all_for_owner(owner_id: str) -> int:
    """Delete all channel-link rows for an owner. Returns count deleted.

    Thin alias over ``sweep_by_owner`` so the e2e teardown endpoint can
    use a uniform ``delete_all_for_owner`` API across every per-user repo.
    """
    return await sweep_by_owner(owner_id)


async def count_for_owner(owner_id: str) -> int:
    """Count channel-link rows for an owner. Used by /debug/ddb-rows.

    Counts every link row (across all providers/agents/peers) for this
    container owner. Does not paginate — same constraint as the existing
    ``query_by_owner`` helper above (per-owner link counts fit in a single
    1MB query page in practice).
    """
    table = _get_table()
    response = await run_in_thread(
        table.query,
        KeyConditionExpression=Key("owner_id").eq(owner_id),
        Select="COUNT",
    )
    return int(response.get("Count", 0))


async def sweep_by_member(member_id: str) -> int:
    """Delete all link rows for a Clerk member. Used by Clerk user.deleted webhook.

    Queries the by-member GSI, then deletes via the main table keys.
    Returns the number of rows deleted.
    """
    items = await query_by_member(member_id)
    await _batch_delete(_get_table(), items)
    return len(items)
