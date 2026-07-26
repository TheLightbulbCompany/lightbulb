"""Container repository -- DynamoDB operations for the containers table."""

import logging
import time
import uuid

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)

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

    Used by fleet-wide ops tools (ecs_ops, bedrock_gate_break_glass).
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

    Also preserves the FULL provision lease (``provision_claim_id`` +
    ``provision_claimed_at`` + ``provision_attempt_token`` +
    ``provision_execution_name``): this put_item REPLACES the whole item, and
    create_user_service upserts mid-provision — dropping the lease here would
    make the in-flight provision look stale/unowned and re-claimable while it
    is still running. The SFN fields matter just as much as the claim:
    dropping ``provision_execution_name`` blinds Branch-A redispatch to the
    live execution, and dropping ``provision_attempt_token`` makes every
    attempt-token fence permissive (they all allow
    ``attribute_not_exists``), letting a superseded straggler pass its next
    checkpoint. (PR 2 replaces this whole-item write with a scoped
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
    for lease_field in (
        "provision_claim_id",
        "provision_claimed_at",
        "provision_attempt_token",
        "provision_execution_name",
    ):
        if existing and lease_field in existing and lease_field not in fields:
            item[lease_field] = existing[lease_field]
    # Ensure owner_id is not overridden by fields
    item["owner_id"] = owner_id

    await run_in_thread(table.put_item, Item=item)
    publish_status_changed(owner_id)
    return item


async def try_claim_provision(
    owner_id: str,
    *,
    claim_id: str | None = None,
    execution_name: str | None = None,
) -> str | None:
    """Atomically claim the right to provision this owner's container. Returns
    the fencing token (``provision_claim_id``) iff THIS caller won; None when a
    provision is already in flight.

    THE RACE THIS CLOSES: a fresh owner's ``GET /container/status`` is polled
    every ~3s, and the container row historically wasn't written until deep
    inside the provision (create_user_service), AFTER the Bedrock AIP mint. So
    two polls (a page refresh mid-provision) both saw "no row", both dispatched
    ``dispatch_provision``, and the two full provisions interleaved — each
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

    The SFN dispatcher (``core.services.provision_dispatch``) passes
    ``claim_id`` + ``execution_name`` so the execution name is stamped in the
    same atomic write as the claim — a crash between claim and StartExecution
    is healable by name (see ``provision_dispatch`` module docstring, Branch
    A). It also stamps ``provision_attempt_token = claim_id`` -- attempt 0's
    fencing token. A retried SFN attempt (``BumpAttempt`` in the state
    machine) mints a FRESH attempt token via ``claim_attempt`` below; that is
    what fences a straggler from a still-running previous attempt out at its
    next checkpoint, while the claim/execution name stay stable across
    retries (see ``claim_attempt`` docstring).
    """
    table = _get_table()
    claim_id = claim_id or str(uuid.uuid4())
    now_epoch = int(time.time())
    now_iso = utc_now_iso()
    update_expr = (
        "SET #s = :prov, substatus = :sub, provision_claim_id = :cid, "
        "provision_attempt_token = :cid, "
        "provision_claimed_at = :now, updated_at = :iso, "
        "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
    )
    values = {
        ":prov": "provisioning",
        ":sub": "claimed",
        ":cid": claim_id,
        ":now": now_epoch,
        ":iso": now_iso,
        ":newid": str(uuid.uuid4()),
        ":error": "error",
        ":stopped": "stopped",
        ":stale": now_epoch - PROVISION_CLAIM_STALE_S,
    }
    if execution_name is not None:
        update_expr += ", provision_execution_name = :ename"
        values[":ename"] = execution_name
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=update_expr,
            ConditionExpression=(
                "attribute_not_exists(owner_id) OR #s = :error OR #s = :stopped "
                "OR (#s = :prov AND (attribute_not_exists(provision_claimed_at) OR provision_claimed_at < :stale))"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues=values,
        )
        return claim_id
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def try_consume_daily_email_send(owner_id: str, *, day: str, cap: int) -> bool:
    """Reserve one agent-initiated owner email for ``day`` (UTC ``YYYY-MM-DD``).

    DURABLE on purpose. This is the only fence against an agent looping on the
    owner-send endpoint, and the backend runs several ECS tasks behind an ALB —
    an in-process bucket (the pattern ``agent_mail_relay`` uses for burst
    smoothing) would give a real limit of ``cap * task_count`` and reset on
    every deploy.

    Two conditional writes, at most one extra round trip:

    1. Roll over — set the counter to 1 when the stored day is absent or stale.
    2. Same day — increment, conditional on the count still being under cap.

    Returns True when the send is allowed.

    **Fails OPEN.** A DynamoDB fault returns True rather than blocking. The
    once-only first-contact guard and the per-minute burst limiter both still
    apply, so the blast radius of failing open is bounded — whereas failing
    closed would silently swallow a legitimate first-contact email, which is
    the exact failure mode this whole feature exists to stop.
    """
    table = _get_table()

    # 1. New day, or first send ever: reset the window to 1.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET email_send_day = :d, email_send_count = :one",
            ConditionExpression=(
                "attribute_exists(owner_id) AND (attribute_not_exists(email_send_day) OR email_send_day <> :d)"
            ),
            ExpressionAttributeValues={":d": day, ":one": 1},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            return True  # fail open — see docstring

    # 2. Same day: increment only while under cap.
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET email_send_count = email_send_count + :one",
            ConditionExpression="email_send_day = :d AND email_send_count < :cap",
            ExpressionAttributeValues={":d": day, ":one": 1, ":cap": cap},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        return True  # fail open — see docstring


async def claim_attempt(owner_id: str, claim_id: str, attempt_token: str) -> None:
    """Stamp a fresh per-SFN-retry ``provision_attempt_token`` onto an
    already-claimed lease. Called by ``scripts/provision_runner`` as its
    FIRST action, every attempt (including attempt 0, where it is a
    same-value no-op re-assertion).

    THE HAZARD THIS CLOSES: the state machine's per-attempt task timeout does
    NOT guarantee the previous attempt's ECS task is dead (a network
    partition can leave SFN's wait timing out while the task keeps running).
    A naive retry that reused the SAME fencing value for both the straggler
    and the new attempt would let both concurrently pass
    ``update_container_leased``'s checkpoint and interleave their writes --
    exactly the double-provision class this whole lease design exists to
    prevent. Re-stamping here, conditioned ONLY on ``provision_claim_id``
    (not on the previous attempt token), immediately invalidates the
    straggler's in-memory token: its NEXT checkpoint call now carries a stale
    ``attempt_token`` that no longer matches the row, so
    ``update_container_leased`` raises ``ProvisionSuperseded`` and the
    straggler stops before its next side effect.

    Deliberately conditioned on claim_id ONLY, not on attempt_token: this is
    the write that ESTABLISHES the new attempt as current, so it cannot
    itself require the new attempt's own (not-yet-written) token to match.
    Raises ``ProvisionSuperseded`` iff the CLAIM itself was taken over
    (a deliberate reprovision door) -- the runner must stop entirely in that
    case, same as any other checkpoint.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET provision_attempt_token = :tok, provision_claimed_at = :now, updated_at = :iso",
            ConditionExpression="provision_claim_id = :cid",
            ExpressionAttributeValues={
                ":tok": attempt_token,
                ":cid": claim_id,
                ":now": int(time.time()),
                ":iso": utc_now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ProvisionSuperseded(f"provision lease for {owner_id} no longer held") from e
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
            "SET provision_claim_id = :cid, provision_attempt_token = :cid, provision_claimed_at = :now, "
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


async def update_container_leased(
    owner_id: str,
    claim_id: str,
    fields: dict | None = None,
    *,
    generation: str | None = None,
    attempt_token: str | None = None,
    release_lease: bool = False,
) -> None:
    """Scoped, fenced row write: applies ``fields`` (may be empty — a pure
    lease-refresh checkpoint) and refreshes the lease stamp, conditioned on
    this run still holding the lease. Raises ``ProvisionSuperseded`` when it
    does not — the caller must stop before its next side effect. These are the
    CHECKPOINTS that make the fencing token real: a zombie provision is stopped
    here, before it can write config or IAM content over the superseding
    run's.

    ``generation`` and ``attempt_token`` are OPTIONAL additional fences, both
    off by default (preserving every existing non-SFN caller's behavior
    unchanged):

    - ``generation`` -- when given, additionally requires the row's
      ``provision_generation`` (the container-incarnation fence, minted once
      at G1 in ``EcsManager.create_user_service``) to match or be absent.
      Belt-and-suspenders against a very late straggler from a PRIOR
      container incarnation (a full delete+recreate, not merely a lease
      retake) writing onto a newer incarnation's row; ``claim_id`` alone
      already fences this in practice since a new incarnation always mints a
      fresh claim, so this is defense-in-depth, not the primary fence.
    - ``attempt_token`` -- when given, additionally requires the row's
      ``provision_attempt_token`` to match. THIS is the primary fence for the
      SFN per-retry-attempt hazard (see ``claim_attempt`` docstring): a
      straggler from a superseded attempt carries a stale token and is
      rejected here.

    ``release_lease``: set on the TERMINAL SUCCESS write (the fenced
    status="running" transition) to atomically REMOVE the lease fields with
    it. This makes "provisioning row carries a claim" a reliable
    active-SFN-work signal — the boot reconcile in main.py keys on exactly
    that to decide which stuck-provisioning rows are its to resume, and
    residual claims from a COMPLETED provision would otherwise misclassify
    a later interrupted wake/resize as SFN-owned forever. NOT used on error
    releases: in-machine retries legitimately sit at status="error" with
    the stamped claim/execution-name, which dispatch Branch A needs intact
    to re-issue the same execution.
    """
    table = _get_table()
    names: dict[str, str] = {}
    set_parts: list[str] = []
    values: dict = {}
    for i, (key, value) in enumerate((fields or {}).items()):
        alias = f"#f{i}"
        names[alias] = key
        set_parts.append(f"{alias} = :v{i}")
        values[f":v{i}"] = value
    if not release_lease:
        set_parts.append("provision_claimed_at = :now")
    set_parts.append("updated_at = :iso")
    condition = "provision_claim_id = :cid"
    values[":cid"] = claim_id
    if generation is not None:
        condition += " AND (attribute_not_exists(provision_generation) OR provision_generation = :gen)"
        values[":gen"] = generation
    if attempt_token is not None:
        condition += " AND (attribute_not_exists(provision_attempt_token) OR provision_attempt_token = :atok)"
        values[":atok"] = attempt_token
    update_expr = "SET " + ", ".join(set_parts)
    if release_lease:
        update_expr += (
            " REMOVE provision_claim_id, provision_claimed_at, provision_attempt_token, provision_execution_name"
        )
    kwargs: dict = {
        "Key": {"owner_id": owner_id},
        "UpdateExpression": update_expr,
        "ConditionExpression": condition,
        "ExpressionAttributeValues": {
            **values,
            **({} if release_lease else {":now": int(time.time())}),
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
    generation: str | None = None,
    attempt_token: str | None = None,
) -> None:
    """Release a held lease by flipping the row to ``error`` — immediately
    re-claimable, and the status poll's existing error auto-retry re-dispatches,
    so a failed provision self-heals on the next ~3s poll.

    SINGLE OWNER of the provision-path ``status="error"`` transition. Every
    synchronous provision failure funnels here (``provision_user_container``'s
    except); the scattered pre-emptive ``status="error"`` writes inside
    ``ecs_manager`` that used to fire first — and no-op this release's
    ``status = provisioning`` condition, dropping the cause — were removed, and
    ``_update_container`` now rejects a ``status="error"`` field to keep it that
    way. ``_await_running_transition``'s rolloutState=FAILED write now ALSO
    routes through here (when called with a ``claim_id``, i.e. the runner's
    foreground ``wait_for_running`` call) rather than writing ``status="error"``
    directly — closing the historical single-owner-rule exception.

    Records the failure CAUSE on the same atomic transition so the status
    endpoint can tell a transient failure (silently retried) from a
    deterministic one (surfaced to the owner instead of looped):
    - ``last_error`` — a short human-readable cause (truncated). Omitted from
      the write when None, so a bare release still lands a clean error row.
    - ``retryable`` — transient (True: retry) vs deterministic (False: surface).
      Always written; defaults to True so a cause-less caller leaves the row in
      the safe self-healing state (the pre-existing behaviour).
    - ``generation`` / ``attempt_token`` — optional additional fences, same
      semantics as ``update_container_leased`` (see its docstring). Off by
      default so every existing non-SFN caller is unchanged.

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
    condition = "provision_claim_id = :cid AND #s = :prov"
    if generation is not None:
        condition += " AND (attribute_not_exists(provision_generation) OR provision_generation = :gen)"
        values[":gen"] = generation
    if attempt_token is not None:
        condition += " AND (attribute_not_exists(provision_attempt_token) OR provision_attempt_token = :atok)"
        values[":atok"] = attempt_token
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression=condition,
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


# The only row fields the trial->paid conversion marker is allowed to touch.
# ``update_conversion_fields`` enforces this allowlist so no future caller can
# quietly grow it into a status/lease writer (the clobber class it exists to
# close).
_CONVERSION_MARKER_FIELDS = frozenset({"conversion_pending", "substatus", "conversion_started_at"})


async def update_conversion_fields(owner_id: str, fields: dict) -> None:
    """Scoped, lease-safe write of the trial->paid conversion marker fields
    onto an EXISTING containers row.

    NEVER the get→put ``update_fields`` — that was a live incident: the
    conversion's release write (``run_conversion_reprovision``) read the row
    BEFORE its own freshly dispatched provision won ``try_claim_provision``,
    then put the whole stale item back — resurrecting ``status="stopped"``
    over the just-claimed "provisioning" AND erasing every lease field
    (``provision_claim_id`` / ``provision_attempt_token`` /
    ``provision_execution_name``). The already-started SFN runner then hit
    ``ProvisionSuperseded`` at its first checkpoint and stood down, leaving a
    PAYING owner stopped with nothing running and nothing scheduled. A scoped
    ``update_item`` on exactly the marker fields cannot touch ``status`` or
    the lease, so the race collapses to a cosmetic substatus overwrite at
    worst (the claim's "claimed" label may be nulled for one poll; the next
    checkpoint/status write refreshes it).

    ``fields`` may only contain conversion marker keys (allowlist above);
    anything else raises ``ValueError`` — this helper must never become a
    general row writer. ``None`` values are written as DynamoDB NULL,
    matching what the old put-based path stored (readers ``.get(...)`` both
    shapes identically).

    Conditioned on the row existing; a concurrently deleted row makes this a
    silent no-op (nothing left to mark), mirroring ``stamp_stop_reason``.
    """
    unknown = set(fields) - _CONVERSION_MARKER_FIELDS
    if unknown:
        raise ValueError(f"update_conversion_fields: disallowed fields {sorted(unknown)}")
    table = _get_table()
    names: dict[str, str] = {}
    set_parts: list[str] = []
    values: dict = {":iso": utc_now_iso()}
    for i, (key, value) in enumerate(fields.items()):
        alias = f"#f{i}"
        names[alias] = key
        set_parts.append(f"{alias} = :v{i}")
        values[f":v{i}"] = value
    set_parts.append("updated_at = :iso")
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET " + ", ".join(set_parts),
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return
    publish_status_changed(owner_id)


async def release_stranded_stop_to_error(owner_id: str, *, last_error: str) -> bool:
    """CAS: flip a claim-less ``stopped`` row to ``error`` (retryable) so the
    status poll's ordinary error auto-retry re-dispatches it.

    The shape this targets is the superseded-without-successor strand: an SFN
    runner detects supersession (its claim no longer on the row) and stands
    down, but a re-read shows NO successor claim either — nobody is running
    and nothing is scheduled. ``release_provision_to_error`` cannot land here
    (it is conditioned on the CALLER's claim still matching AND
    ``status="provisioning"``; the stranded row has neither), so this sibling
    conditions on exactly the illegal steady-state instead:

    - ``status = "stopped"``      — the observed strand. "error" rows already
      self-heal via the poll (retryable defaults True there), and an
      UNCLAIMED "provisioning" row belongs to a wake/resize transition
      (those paths never claim — see main.py's boot resume) and must not be
      clobbered under its in-process poller.
    - ``attribute_not_exists(provision_claim_id)`` — a present claim means a
      successor exists; its liveness is Branch A / stale-lease territory,
      not ours.
    - ``stop_reason <> "trial_idle"`` (or absent) — a deliberately parked
      trial box keeps its wake-gated semantics; it must never be error-flipped
      into an unconditional poll retry. (The payment/trial gates on the poll's
      retry branch make this belt-and-suspenders, but the condition keeps the
      flip honest at the source.)

    All three clauses evaluate atomically at write time — no read-then-write
    gap for a racing claim to slip through. Returns True iff the flip landed;
    False when the row moved on (claimed, deleted, restarted) — which is
    exactly the "a successor showed up after all" answer the caller wants.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #s = :err, substatus = :sub, #rt = :true, #le = :lasterr, updated_at = :iso",
            ConditionExpression=(
                "#s = :stopped AND attribute_not_exists(provision_claim_id) "
                "AND (attribute_not_exists(stop_reason) OR stop_reason <> :trial_idle)"
            ),
            ExpressionAttributeNames={"#s": "status", "#rt": "retryable", "#le": "last_error"},
            ExpressionAttributeValues={
                ":err": "error",
                ":sub": "provision_failed",
                ":stopped": "stopped",
                ":trial_idle": "trial_idle",
                ":true": True,
                ":lasterr": str(last_error)[:500],
                ":iso": utc_now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    publish_status_changed(owner_id)
    return True


def classify_provision_error(exc: BaseException) -> tuple[str, bool]:
    """Map a caught provision failure to ``(last_error, retryable)`` for
    ``release_provision_to_error``.

    Lives here (not in a service) because it is shared by both callers of that
    release — ``services.provision_dispatch.dispatch_provision`` (the
    poll-driven path) and ``containers.ecs_manager.provision_user_container``
    (the deliberate-reprovision doors) — and this repository already owns the
    error transition + fields, so both can import it without an import cycle.

    ``retryable`` answers whether re-running the identical provision could
    plausibly succeed: a transient infra failure (ECS Spot-reclamation,
    throttling, 5xx, ENI-attach) self-heals on the poll's next ~3s auto-retry,
    while a deterministic one (a 4xx Paperclip / config / validation error)
    fails identically forever and must be surfaced instead of looped.

    - An exception's own ``retryable`` self-classification takes precedence
      over everything below — ``PaperclipApiError`` sets it off its status
      code (5xx/429 transient, 4xx deterministic) and is reused verbatim.
    - ``ValueError`` / ``TypeError`` / ``KeyError`` are deterministic
      config/shape bugs — a retry can't fix a malformed value.
    - A small set of known-deterministic message markers (provider-config
      failures that raise as plain ``Exception``) are matched case-insensitively
      after the attr/type checks, so they still classify deterministic.
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
    retryable_attr = getattr(exc, "retryable", None)
    if retryable_attr is not None:
        return cause, bool(retryable_attr)
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return cause, False
    message = str(exc).lower()
    if "byo_provider is required" in message or "unknown provider_choice" in message:
        return cause, False
    return cause, True


def is_provision_claim_stale(container: dict | None) -> bool:
    """True when a ``provisioning`` row's lease has had no checkpoint refresh
    for PROVISION_CLAIM_STALE_S — the process running it is dead. Used by the
    status poll's stale-recovery branch (which additionally checks ECS for a
    live service before re-dispatching, so a slow-but-healthy post-start boot
    is never re-provisioned on top of).

    INTERIM BRIDGE, not deleted by the SFN cutover (PR-10): dispatching
    through ``core.services.provision_dispatch.dispatch_provision`` when this
    fires is now SAFE unconditionally (StartExecution is idempotent by
    execution name, Branch A of the dispatcher), but it only catches a wedge
    when SOMETHING re-polls a stale-lease row. The design's real fix for the
    "SFN execution closed without a terminal row write" case is the
    ``reconciliation_worker`` (SWEEP, not yet built — see
    ``core.services.provision_dispatch`` module docstring) actively
    disambiguating via ``DescribeExecution``. Until SWEEP lands, deleting this
    wall-clock heuristic would leave that wedge with NO automatic recovery
    path at all (only a manual ``/retry`` or ``/recover``) — a net regression
    with no replacement — so it stays, on a wall-clock timer, doing the same
    job it did for the inline executor. Superseded (not merely duplicated) by
    SWEEP once that lands; remove this heuristic then.
    """
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
    publish_status_changed(owner_id)
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


async def stamp_email_cold_wake(owner_id: str) -> None:
    """Record that an inbound email cold-woke this container.

    Read once by ``agent_mail_reply.send_reply`` to decide whether the agent's
    outgoing reply should carry the free-tier "I was asleep" footer. The wake
    and the reply happen in DIFFERENT requests, which is why this has to be
    persisted rather than passed. Never raises.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET email_cold_wake_at = :t",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":t": int(time.time())},
        )
    except ClientError as e:
        logger.warning("stamp_email_cold_wake failed for %s: %s", owner_id, e)


async def consume_email_cold_wake(owner_id: str, *, max_age_seconds: int = 3600) -> bool:
    """True when a RECENT cold-wake stamp existed. Clears it either way.

    ONE conditional write, not read-then-remove: two concurrent callers for
    the same owner could otherwise both read the stamp as fresh before either
    cleared it, attaching the "I was asleep" footer to two separate replies.
    The condition (``attribute_exists(email_cold_wake_at)``) means only one
    concurrent caller can ever satisfy it; the loser gets
    ConditionalCheckFailedException and correctly reports False. Clearing
    unconditionally on both the fresh AND stale path is a side effect of this
    being the same write that reads the value — a stale stamp must not attach
    to an unrelated reply days later. Never raises.
    """
    table = _get_table()
    try:
        resp = await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE email_cold_wake_at",
            ConditionExpression="attribute_exists(owner_id) AND attribute_exists(email_cold_wake_at)",
            ReturnValues="UPDATED_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("consume_email_cold_wake failed for %s: %s", owner_id, e)
        return False

    stamped = (resp.get("Attributes") or {}).get("email_cold_wake_at")
    if stamped is None:
        return False
    return (int(time.time()) - int(stamped)) < max_age_seconds
