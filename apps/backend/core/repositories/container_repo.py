"""Container repository -- DynamoDB operations for the containers table."""

import time
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso

# How long a provision lease (provision_claim_id + provision_claimed_at) stays
# exclusive with no checkpoint refresh before it is considered abandoned. Every
# leased checkpoint write refreshes the stamp, so this measures the gap BETWEEN
# steps of a live provision (seconds), not the total provision time — 300s only
# elapses when the process died mid-provision. The post-start boot phase (up to
# ~12 min) is deliberately NOT covered by the lease: the stale-recovery path in
# routers/container.py checks ECS for a live service before re-claiming, so a
# slow-but-healthy boot is never re-provisioned on top of.
PROVISION_CLAIM_STALE_S = 300


class ProvisionSuperseded(Exception):
    """This run's provision lease was taken over by a newer run.

    Raised by ``update_container_leased`` when the row's ``provision_claim_id``
    no longer matches — a deliberate reprovision (conversion, /recover, admin)
    has taken over, or a stale lease was re-claimed. The superseded run must
    stop before its next side effect; it must NOT write an error status (the
    row now belongs to the superseding run)."""


def _get_table():
    return get_table("containers")


async def get_by_owner_id(owner_id: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id})
    return response.get("Item")


async def get_by_gateway_token(token: str) -> dict | None:
    table = _get_table()
    response = await run_in_thread(
        table.query,
        IndexName="gateway-token-index",
        KeyConditionExpression=Key("gateway_token").eq(token),
    )
    items = response.get("Items", [])
    return items[0] if items else None


async def get_by_status(status: str) -> list[dict]:
    """Return every container row with the given status.

    Follows DynamoDB's LastEvaluatedKey. Query responses are capped at 1 MB,
    so a single call only returns the first page — the reaper (and any other
    caller that iterates the full set for correctness) needs every row.
    """
    table = _get_table()
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs = {
            "IndexName": "status-index",
            "KeyConditionExpression": Key("status").eq(status),
        }
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = await run_in_thread(table.query, **kwargs)
        items.extend(response.get("Items", []))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def scan_all() -> list[dict]:
    """Return every container row. Paginates via DDB LastEvaluatedKey.

    Used by the one-shot hook-token backfill (scripts/backfill-hook-tokens.py).
    Not for hot paths -- DDB Scan is expensive at scale.
    """
    table = _get_table()
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = await run_in_thread(table.scan, **kwargs)
        items.extend(response.get("Items", []))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


async def upsert(owner_id: str, fields: dict) -> dict:
    """Create or update a container record. Preserves id and created_at on existing items.

    Also preserves the provision lease (``provision_claim_id`` +
    ``provision_claimed_at``): this put_item REPLACES the whole item, and
    create_user_service upserts mid-provision — dropping the lease here would
    make the in-flight provision look stale/unowned and re-claimable while it
    is still running. (PR 2 replaces this whole-item write with a scoped
    UpdateItem, which removes the need to preserve anything.)
    """
    table = _get_table()
    existing = await get_by_owner_id(owner_id)

    now = utc_now_iso()
    item = {
        "owner_id": owner_id,
        "id": existing["id"] if existing else str(uuid.uuid4()),
        "created_at": existing["created_at"] if existing else now,
        "updated_at": now,
        **fields,
    }
    for lease_field in ("provision_claim_id", "provision_claimed_at"):
        if existing and lease_field in existing and lease_field not in fields:
            item[lease_field] = existing[lease_field]
    # Ensure owner_id is not overridden by fields
    item["owner_id"] = owner_id

    await run_in_thread(table.put_item, Item=item)
    return item


async def try_claim_provision(owner_id: str) -> str | None:
    """Atomically claim the right to provision this owner's container. Returns
    the fencing token (``provision_claim_id``) iff THIS caller won; None when a
    provision is already in flight.

    THE RACE THIS CLOSES: a fresh owner's ``GET /container/status`` is polled
    every ~3s, and the container row historically wasn't written until deep
    inside the provision (create_user_service), AFTER the Bedrock AIP mint. So
    two polls (a page refresh mid-provision) both saw "no row", both dispatched
    ``background_provision``, and the two full provisions interleaved — each
    minted its own inference profile (Bedrock does not enforce name-uniqueness)
    and one's ARN landed in the container config while the other's landed in
    the IAM policy: AccessDenied on every model call. Seen live on prod.

    This makes the row's creation itself the gate: the dispatch's FIRST act is
    an atomic conditional write that creates/updates the row to
    ``status="provisioning"`` and stamps the lease. DynamoDB serializes it
    across ALL backend tasks — which matters even at desiredCount=1, because
    rolling deploys run at maximumPercent=200, so two backend tasks coexist
    during every deploy. An in-process lock (the ``_org_provision_locks``
    pattern) would reopen the race in that window.

    The returned claim_id is a FENCING TOKEN: the provision threads it through
    every subsequent row write (``update_container_leased``), so a run whose
    lease was taken over aborts at its next checkpoint instead of poisoning the
    winner's writes.

    Claimable: no row, ``error``, ``stopped``, or a ``provisioning`` row whose
    lease is stale (``provision_claimed_at`` absent — legacy/wedged rows — or
    older than PROVISION_CLAIM_STALE_S with no checkpoint refresh). NOT
    claimable: ``running``, or a fresh ``provisioning`` lease. Release is by
    normal status transitions: failure paths flip the row to ``error``
    (immediately re-claimable, and the status poll's existing error auto-retry
    re-dispatches), success ends at ``running``.
    """
    table = _get_table()
    claim_id = str(uuid.uuid4())
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET #s = :prov, substatus = :sub, provision_claim_id = :cid, "
                "provision_claimed_at = :now, updated_at = :iso, "
                "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
            ),
            ConditionExpression=(
                "attribute_not_exists(owner_id) OR #s = :error OR #s = :stopped "
                "OR (#s = :prov AND (attribute_not_exists(provision_claimed_at) OR provision_claimed_at < :stale))"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":prov": "provisioning",
                ":sub": "claimed",
                ":cid": claim_id,
                ":now": now_epoch,
                ":iso": now_iso,
                ":newid": str(uuid.uuid4()),
                ":error": "error",
                ":stopped": "stopped",
                ":stale": now_epoch - PROVISION_CLAIM_STALE_S,
            },
        )
        return claim_id
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def takeover_provision(owner_id: str) -> str:
    """Unconditionally take over the provision lease — deliberate doors only
    (trial->paid conversion, /container/recover, admin force-apply, provider
    change, debug). Returns the new fencing token.

    Never blocked: a deliberate reprovision must always win. Any in-flight
    poll-driven provision is fenced out at its next leased checkpoint (its
    claim_id no longer matches). Only the LEASE fields are stamped — the row's
    ``status`` is preserved (created as "provisioning" if the row is new), so a
    takeover on a running container does not flip the UI into the provisioning
    stepper before the reprovision itself decides what to write.
    """
    table = _get_table()
    claim_id = str(uuid.uuid4())
    now_iso = utc_now_iso()
    await run_in_thread(
        table.update_item,
        Key={"owner_id": owner_id},
        UpdateExpression=(
            "SET provision_claim_id = :cid, provision_claimed_at = :now, "
            "updated_at = :iso, #s = if_not_exists(#s, :prov), "
            "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
        ),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":cid": claim_id,
            ":now": int(time.time()),
            ":iso": now_iso,
            ":prov": "provisioning",
            ":newid": str(uuid.uuid4()),
        },
    )
    return claim_id


async def update_container_leased(owner_id: str, claim_id: str, fields: dict | None = None) -> None:
    """Scoped, fenced row write: applies ``fields`` (may be empty — a pure
    lease-refresh checkpoint) and refreshes the lease stamp, conditioned on
    this run still holding the lease. Raises ``ProvisionSuperseded`` when it
    does not — the caller must stop before its next side effect. These are the
    CHECKPOINTS that make the fencing token real: a zombie provision is stopped
    here, before it can write config or IAM content over the superseding
    run's."""
    table = _get_table()
    names: dict[str, str] = {}
    set_parts: list[str] = []
    values: dict = {}
    for i, (key, value) in enumerate((fields or {}).items()):
        alias = f"#f{i}"
        names[alias] = key
        set_parts.append(f"{alias} = :v{i}")
        values[f":v{i}"] = value
    set_parts.append("provision_claimed_at = :now")
    set_parts.append("updated_at = :iso")
    kwargs: dict = {
        "Key": {"owner_id": owner_id},
        "UpdateExpression": "SET " + ", ".join(set_parts),
        "ConditionExpression": "provision_claim_id = :cid",
        "ExpressionAttributeValues": {
            **values,
            ":cid": claim_id,
            ":now": int(time.time()),
            ":iso": utc_now_iso(),
        },
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    try:
        await run_in_thread(table.update_item, **kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ProvisionSuperseded(f"provision lease for {owner_id} no longer held") from e
        raise


async def release_provision_to_error(
    owner_id: str,
    claim_id: str,
    *,
    last_error: str | None = None,
    retryable: bool = True,
) -> None:
    """Release a held lease by flipping the row to ``error`` — immediately
    re-claimable, and the status poll's existing error auto-retry re-dispatches,
    so a failed provision self-heals on the next ~3s poll.

    Records the failure CAUSE on the same atomic transition so the status
    endpoint can tell a transient failure (silently retried) from a
    deterministic one (surfaced to the owner instead of looped):
    - ``last_error`` — a short human-readable cause (truncated). Omitted from
      the write when None, so a bare release still lands a clean error row.
    - ``retryable`` — transient (True: retry) vs deterministic (False: surface).
      Always written; defaults to True so a cause-less caller leaves the row in
      the safe self-healing state (the pre-existing behaviour).

    Both fields are ADDITIVE — the write stays conditioned on the fencing token
    AND ``status = "provisioning"`` exactly as before, a silent no-op on either
    miss:
    - on the fencing token — if the lease was taken over, the row belongs to
      the superseding run and must not be clobbered with error;
    - on ``status = "provisioning"`` — a failed DELIBERATE reprovision of a
      still-RUNNING container (e.g. a force-redeploy that raised) must leave
      the row "running": the container is, in fact, still running, and an
      error write would send the status poll into a needless reprovision loop.
    """
    table = _get_table()
    # `retryable` and `last_error` are aliased (ExpressionAttributeNames) purely
    # for reserved-word safety, matching the `#s`=status precedent above.
    set_parts = ["#s = :err", "substatus = :sub", "updated_at = :iso", "#rt = :retryable"]
    names = {"#s": "status", "#rt": "retryable"}
    values: dict = {
        ":err": "error",
        ":sub": "provision_failed",
        ":cid": claim_id,
        ":prov": "provisioning",
        ":iso": utc_now_iso(),
        ":retryable": retryable,
    }
    if last_error is not None:
        set_parts.append("#le = :lasterr")
        names["#le"] = "last_error"
        values[":lasterr"] = str(last_error)[:500]
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="provision_claim_id = :cid AND #s = :prov",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def classify_provision_error(exc: BaseException) -> tuple[str, bool]:
    """Map a caught provision failure to ``(last_error, retryable)`` for
    ``release_provision_to_error``.

    Lives here (not in a service) because it is shared by both callers of that
    release — ``services.container_provision.background_provision`` (the
    poll-driven path) and ``containers.ecs_manager.provision_user_container``
    (the deliberate-reprovision doors) — and this repository already owns the
    error transition + fields, so both can import it without an import cycle.

    ``retryable`` answers whether re-running the identical provision could
    plausibly succeed: a transient infra failure (ECS Spot-reclamation,
    throttling, 5xx, ENI-attach) self-heals on the poll's next ~3s auto-retry,
    while a deterministic one (a 4xx Paperclip / config / validation error)
    fails identically forever and must be surfaced instead of looped.

    - ``PaperclipApiError`` already auto-classifies retryability off its status
      code (5xx/429 transient, 4xx deterministic) — reuse ``.retryable`` verbatim.
    - ``ValueError`` / ``TypeError`` / ``KeyError`` are deterministic
      config/shape bugs — a retry can't fix a malformed value.
    - Everything else — notably ``EcsManagerError``, which wraps AWS
      Spot/throttle/capacity ``ClientError``s as a string, plus any unrecognised
      failure — defaults to retryable: a needless retry is cheaper than a
      wrongly-surfaced permanent error.
    """
    # Lazy import: keep this repository module free of the paperclip client's
    # import graph at load time (matches the function-local import style used
    # for cross-layer references elsewhere in the provision path).
    from core.services.paperclip_admin_client import PaperclipApiError

    cause = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, PaperclipApiError):
        return f"Paperclip {exc.status_code}: {exc}", exc.retryable
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return cause, False
    return cause, True


def is_provision_claim_stale(container: dict | None) -> bool:
    """True when a ``provisioning`` row's lease has had no checkpoint refresh
    for PROVISION_CLAIM_STALE_S — the process running it is dead. Used by the
    status poll's stale-recovery branch (which additionally checks ECS for a
    live service before re-dispatching, so a slow-but-healthy post-start boot
    is never re-provisioned on top of)."""
    if not container or container.get("status") != "provisioning":
        return False
    claimed_at = container.get("provision_claimed_at")
    if claimed_at is None:
        return True  # legacy/wedged provisioning row from before the lease existed
    return int(claimed_at) < int(time.time()) - PROVISION_CLAIM_STALE_S


async def update_status(owner_id: str, status: str, substatus: str | None = None) -> dict | None:
    fields = {"status": status, "substatus": substatus}
    return await update_fields(owner_id, fields)


async def update_fields(owner_id: str, fields: dict) -> dict | None:
    existing = await get_by_owner_id(owner_id)
    if existing is None:
        return None

    existing.update(fields)
    existing["updated_at"] = utc_now_iso()

    table = _get_table()
    await run_in_thread(table.put_item, Item=existing)
    return existing


async def stamp_stop_reason(owner_id: str, reason: str) -> None:
    """Scoped write of stop provenance onto an existing row.

    ``stop_reason`` distinguishes a deliberately parked trial box
    (``"trial_idle"``, which also stamps ``parked_at``) from every other way a
    row lands on ``status="stopped"`` (admin stop, killswitch, conversion
    drain). The wake-suppression predicate keys on it, so a crashed or
    admin-stopped container can never be mistaken for parked and lose its
    poll-driven self-heal.

    A scoped ``update_item`` SET — never the get→put ``update_fields``: this
    stamp can race a concurrent wake's ``try_claim_provision``, and a full-item
    put here could resurrect ``status="stopped"`` over a freshly claimed lease
    (the documented double-provision class). Condition on the row existing so a
    concurrent delete never turns the stamp into a ghost row; that miss is
    swallowed (nothing left to stamp).
    """
    table = _get_table()
    expr = "SET stop_reason = :r, updated_at = :iso"
    if reason == "trial_idle":
        expr += ", parked_at = :iso"
    else:
        expr += " REMOVE parked_at"
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=expr,
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":r": reason, ":iso": utc_now_iso()},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def record_wake_debit(owner_id: str, date: str) -> int:
    """Atomically count a legacy (un-flagged) poll wake of a parked row for
    the given UTC ``date`` and return the new day count.

    Two scoped ``update_item`` steps (ensure the parent map, then increment the
    day key via ``SET ... if_not_exists(...) + :one``) — the same shape as the
    bedrock-gate ledger's ``reserved_microcents``/``spent`` nested-map
    accounting, and never the get→put ``update_fields``, which racing
    ``try_claim_provision`` could resurrect a stale ``status`` over a claimed
    lease. The increment is a nested SET, NOT ``ADD``: DynamoDB's ADD action
    only targets top-level Number/Set attributes, so ``ADD wake_debits.#d``
    would raise ValidationException and fail the debit open. Both steps
    condition on the row existing; a missing row returns 0 debits (nothing to
    bound).
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET wake_debits = if_not_exists(wake_debits, :empty)",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":empty": {}},
        )
        result = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            # SET ... if_not_exists(...) + :one — NOT `ADD wake_debits.#d`.
            # DynamoDB's ADD only targets top-level Number/Set attributes; on a
            # nested map path it raises ValidationException, which would fail the
            # whole debit open (legacy_wake_allowed swallows the error and
            # returns True) — silently disabling the budget. The parent map is
            # guaranteed present by the ensure step above, so the nested SET is
            # safe. Mirrors the bedrock-gate ledger's reserved_microcents form.
            UpdateExpression="SET wake_debits.#d = if_not_exists(wake_debits.#d, :zero) + :one, updated_at = :iso",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames={"#d": date},
            ExpressionAttributeValues={":one": 1, ":zero": 0, ":iso": utc_now_iso()},
            ReturnValues="UPDATED_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return 0
        raise
    day = (result.get("Attributes") or {}).get("wake_debits", {}).get(date, 0)
    return int(day)


async def upsert_paired_devices(owner_id: str, entries: dict) -> None:
    """Project desktop device-pubkey entries onto the owner row's ``paired_devices`` map.

    ``entries`` is ``{deviceId: {"publicKey": <b64url-raw>, "userId": <member>,
    "clientId": <str>}}``. This is the authorizer-reachable projection of the
    device pubkeys that also live in the container's EFS ``paired.json`` — it
    gives the WebSocket Lambda authorizer a ``GetItem(containers, owner)`` point
    lookup for device-key WS-upgrade auth, without a separate table.

    Each device is written with a SCOPED, server-atomic ``UpdateItem`` (touching
    only its one map key) rather than a full-item read-modify-write — so a
    concurrent status/hook-token/config writer, or a concurrent write of another
    device, cannot clobber it or resurrect a revoked device. DynamoDB serializes
    item-level UpdateItems.

    NON-RELABEL guard (atomic): ``deviceId = sha256(pubkey)`` and POST /desktop/pair
    does NOT verify private-key possession, so a co-tenant could submit another
    member's PUBLIC key (freely observable) and collide their deviceId. The
    ``ConditionExpression`` only writes when the device is absent OR already owned
    by the SAME member — so one org member can never relabel another's device on
    the authorizer's trust anchor (mirrors the paired.json guard in desktop_pair.py).
    No-op if the container row doesn't exist.
    """
    table = _get_table()
    for device_id, entry in entries.items():
        await _put_paired_device(table, owner_id, device_id, entry)


async def _put_paired_device(table, owner_id: str, device_id: str, entry: dict) -> None:
    # 1. Ensure the paired_devices map exists — a nested-path SET below fails if
    #    its parent map is absent. Idempotent; only touches an existing row.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #pd = if_not_exists(#pd, :empty)",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames={"#pd": "paired_devices"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no container row for this owner — nothing to project onto
        raise
    # 2. Scoped conditional set of the single map key. Condition = row still
    #    exists (guards against a concurrent deprovision-delete landing between
    #    step 1 and here, which would otherwise upsert a partial zombie row)
    #    AND (device absent OR already ours -> refuse to relabel another
    #    member's device). Both clauses are evaluated atomically at write time.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #pd.#did = :entry",
            ConditionExpression=(
                "attribute_exists(owner_id) AND (attribute_not_exists(#pd.#did) OR #pd.#did.#uid = :uid)"
            ),
            ExpressionAttributeNames={
                "#pd": "paired_devices",
                "#did": device_id,
                "#uid": "userId",
            },
            ExpressionAttributeValues={":entry": entry, ":uid": entry.get("userId")},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # row deleted mid-write, or deviceId owned by another member — no-op
        raise


async def remove_paired_device(owner_id: str, device_id: str) -> None:
    """Remove a device from the owner row's ``paired_devices`` map (revocation).

    Scoped ``REMOVE`` of the single map key so a racing writer of other row
    attributes cannot resurrect the revoked entry. No-op if the row is absent.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE #pd.#did",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames={"#pd": "paired_devices", "#did": device_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no container row — nothing to remove
        raise


async def set_hook_token(owner_id: str, token: str) -> dict | None:
    """Persist a freshly generated OpenClaw hook token onto the containers row.

    Sets ``hook_token`` and ``hook_token_rotated_at`` together so the rotation
    timestamp always tracks the current token. Returns the updated row, or
    None if the row does not exist.
    """
    return await update_fields(
        owner_id,
        {"hook_token": token, "hook_token_rotated_at": utc_now_iso()},
    )


async def mark_hook_token_provisioned(owner_id: str) -> dict | None:
    """Stamp ``hook_token_provisioned_at`` after the full backfill sequence
    (set_hook_token → refresh_task_definition → wait → patch_openclaw_config)
    has succeeded for this user.

    The backfill orchestrator's idempotency guard tests this field, not
    ``hook_token``, so a crash mid-sequence leaves the user retryable.
    """
    return await update_fields(
        owner_id,
        {"hook_token_provisioned_at": utc_now_iso()},
    )


async def update_error(owner_id: str, error: str) -> dict | None:
    """Record the last error for a container."""
    return await update_fields(
        owner_id,
        {
            "last_error": error,
            "last_error_at": utc_now_iso(),
        },
    )


async def delete(owner_id: str) -> None:
    table = _get_table()
    await run_in_thread(table.delete_item, Key={"owner_id": owner_id})
