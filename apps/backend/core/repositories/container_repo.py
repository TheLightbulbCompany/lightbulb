"""Container repository -- DynamoDB operations for the containers table."""

import logging
import time
import uuid

from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from core.dynamodb import get_table, run_in_thread, utc_now_iso
from core.observability.metrics import put_metric
from core.services.status_events import publish_status_changed

logger = logging.getLogger(__name__)

# Deserializes the raw (low-level, AttributeValue-typed) ``Item`` DynamoDB
# hands back on ReturnValuesOnConditionCheckFailure — see _held_listing_id.
_DESERIALIZER = TypeDeserializer()

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


async def get_by_owner_id(owner_id: str, *, consistent: bool = False) -> dict | None:
    """Read the owner's container row. Eventually consistent BY DEFAULT.

    ``consistent=True`` is opt-in per call, never the default: this is the
    hottest read in the system (``GET /container/status`` polls it every ~3s
    per signed-in user), and a strongly consistent GetItem costs double the
    RCUs. Ask for it only where a stale answer is a correctness bug rather than
    a slightly old status — today that is ``upsert``'s read-modify-write, whose
    whole-item put drops any field the read failed to see.
    """
    table = _get_table()
    response = await run_in_thread(table.get_item, Key={"owner_id": owner_id}, ConsistentRead=consistent)
    return response.get("Item")


async def get_by_status(status: str) -> list[dict]:
    """Return every container row with the given status.

    Follows DynamoDB's LastEvaluatedKey. Query responses are capped at 1 MB,
    so a single call only returns the first page — the reaper (and any other
    caller that iterates the full set for correctness) needs every row.

    Needs no pass-admission filter (unlike ``scan_all``): a pass row (see
    ``try_claim_pass``) carries no ``status`` attribute, so it is absent from
    the sparse ``status-index`` GSI in the first place.
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


#: Every non-owner row in this table keys on ``__<what>__#…``. A Clerk owner id is ``org_…``
#: or ``user_…`` and never starts with ``_``, so ``scan_all`` can exclude all of them with one
#: ``begins_with`` -- and a reserved row added later is excluded by construction rather than by
#: remembering to widen a list. ``PASS_KEY_PREFIX`` and ``LIFECYCLE_KEY_PREFIX`` both start here.
RESERVED_KEY_PREFIX = "__"


async def scan_all(*, consistent: bool = False) -> list[dict]:
    """Return every OWNER row. Paginates via DDB LastEvaluatedKey.

    Used by fleet-wide ops tools (the operator console's fleet grid, the break-glass and
    runtime-roll scripts). Not for hot paths -- DDB Scan is expensive at scale.

    Filters out every RESERVED-KEY row (``RESERVED_KEY_PREFIX``: pass admission, the
    owner-lifecycle lease) so no fleet consumer ever mistakes one for an owner's workspace --
    a lease row would otherwise render as a fleet tile and be handed to a roll script. ONE
    guard here instead of one per caller, and one prefix so a new reserved row is covered by
    construction rather than by remembering to widen a list.
    """
    table = _get_table()
    items: list[dict] = []
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict = {"ConsistentRead": True} if consistent else {}
        kwargs["FilterExpression"] = "NOT begins_with(owner_id, :reserved_prefix)"
        kwargs["ExpressionAttributeValues"] = {":reserved_prefix": RESERVED_KEY_PREFIX}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        response = await run_in_thread(table.scan, **kwargs)
        items.extend(response.get("Items", []))
        last_evaluated_key = response.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return items


# Fields this whole-item put_item must carry over from the existing row when the
# caller did not supply them. See ``upsert``'s docstring for why each is here.
_UPSERT_PRESERVED_FIELDS = (
    "provision_claim_id",
    "provision_claimed_at",
    "provision_attempt_token",
    "provision_execution_name",
    # The bedrock-gate superseded-hash list (``bedrock_gate_tokens._SUPERSEDED_FIELD``
    # — bound to that constant by
    # test_container_repo.test_upsert_preserves_the_bedrock_gate_superseded_hashes,
    # since importing it here would close an import cycle). It is the ONLY
    # handle on an owner's old gate-token rows: that table is hash-keyed with no
    # owner index, so a hash dropped from this list can never be found, deleted,
    # or denied again — leaving a live bearer credential (its plaintext is still
    # in the retained task-definition revision) that nothing can kill.
    "bedrock_gate_superseded_hashes",
    # The pointer to the owner's CURRENT gate token, for the same reason. Both
    # gate fields move ONLY through ``swap_gate_token_hash``, so no caller
    # passes either one here and this put can only ever PRESERVE them — but
    # preserve it must: a reprovision through ``create_user_service`` (that is
    # ``routers/debug.create_user_service(owner_id, gateway_token)``, and a
    # Bedrock -> chatgpt_oauth provider-swap reprovision through the no-service
    # branch) would otherwise strand the token the LIVE container is presenting.
    "bedrock_gate_token_hash",
    # Durable command activity is independently updated by messaging/runtime
    # requests. Preserve it across reprovision and pin it so a stale whole-item
    # put retries instead of erasing a newer command timestamp.
    "last_interactive_at",
)

_RELEASE_OWNED_FIELDS = (
    "desired_release_id",
    "active_release_id",
    "release_revision",
    "release_update_id",
)


# The two fields ``swap_gate_token_hash`` owns outright. Enforced, not merely
# documented: the CAS below conditions on the POINTER, so a caller naming only
# the superseded list would slip past it and silently replace the list with the
# pointer unchanged — and every hash dropped from it is a live bearer credential
# nothing can find, expire, or deny again.
_GATE_OWNED_FIELDS = frozenset({"bedrock_gate_token_hash", "bedrock_gate_superseded_hashes"})


def _reject_gate_owned_fields(caller: str, fields: dict) -> None:
    named = sorted(_GATE_OWNED_FIELDS & fields.keys())
    if named:
        raise ValueError(
            f"{caller}: {named} may only be written by swap_gate_token_hash — a write from "
            "anywhere else can erase a hash the bedrock-gate kill switch still needs to reach"
        )


def _reject_release_owned_fields(caller: str, fields: dict) -> None:
    named = sorted(set(_RELEASE_OWNED_FIELDS) & fields.keys())
    if named:
        raise ValueError(f"{caller}: {named} may only be written by release transactions")


# How many times ``upsert``'s whole-item put re-reads and rebuilds after losing
# the CAS below. One rival run lands at most TWO writes this condition can
# notice — its ``takeover_provision`` (moving the claim) and its
# ``mint_gate_token`` swap (moving the gate pointer) — so 2 + 1 = 3. Its
# ``claim_attempt`` re-stamp and its ``persist_gate_token_hash`` re-assert both
# write values that are already there, which the condition compares by VALUE and
# so does not see. Exhaustion RAISES rather than forcing the write: a failed
# provision is retried by the status poll, an orphaned bearer token and a
# resurrected lease are not self-healing.
_UPSERT_CAS_ATTEMPTS = 3

# The fields ``upsert``'s put pins to the values its read saw. Every one is
# owned by ANOTHER writer and merely preserved here, so writing one back from a
# stale snapshot is a lost update with no self-healing:
#
# - ``bedrock_gate_token_hash`` — resurrecting it drops the hash a concurrent
#   mint wrote. The gate-tokens table is hash-keyed with no owner index, so a
#   hash the row no longer names can never be found, expired, or denied again,
#   while its plaintext is still live in a retained task-definition revision.
# - ``provision_claim_id`` / ``provision_attempt_token`` — resurrecting either
#   re-authorizes a superseded run at its next checkpoint while fencing OUT the
#   successor that legitimately took the lease. That is the double-provision
#   hazard: two tasks writing one EFS workspace, which is how SQLite WAL has
#   been corrupted here before.
#
# Deliberately NOT pinned, because both ride along on a field that is (verified
# by grepping every writer of each; re-verify if you add one):
# - ``provision_execution_name`` — written only by ``try_claim_provision``, in
#   the same atomic write as a fresh ``provision_claim_id``, and removed only by
#   ``update_container_leased(release_lease=True)``, which removes the claim and
#   the attempt token with it. It cannot change without a pinned field changing.
# - ``provision_claimed_at`` — the lease heartbeat. The one writer that moves it
#   ALONE is ``update_container_leased``'s checkpoint, conditioned on the claim
#   we also saw, i.e. the holder we already agree with; restoring a stamp a
#   round-trip old is invisible against PROVISION_CLAIM_STALE_S (300s) and the
#   next checkpoint refreshes it. Every change that MATTERS (try_claim,
#   takeover, attempt bump, lease release) moves the claim or the attempt token
#   in the same atomic write.
#
# So the set is COMPLETE: every preserved field is either pinned here or cannot
# change without one that is.
#
# The two groups are pinned for the same reason but a refusal on each means the
# OPPOSITE thing, which is why they are named separately — see
# ``_raise_if_superseded``.
_LEASE_CAS_FIELDS = ("provision_claim_id", "provision_attempt_token")
_UPSERT_PRESERVED_FIELDS += _RELEASE_OWNED_FIELDS
_UPSERT_CAS_FIELDS = (
    # ``id`` seeds the gate bearer (``derive_gate_bearer``): pinned so an upsert that read no
    # row (or no id) cannot replace the id a concurrent creator already wrote -- the refusal
    # rebuilds on the winner's id.
    "id",
    "bedrock_gate_token_hash",
    "last_interactive_at",
    *_LEASE_CAS_FIELDS,
    *_RELEASE_OWNED_FIELDS,
)


def _unchanged_since_read(existing: dict | None) -> tuple[dict, dict]:
    """``put_item`` kwargs that refuse the write if any pinned field has moved,
    plus the lease/release fields whose movement is a stop signal, in the shape
    ``_raise_if_superseded`` compares.

    Both come out of the one builder deliberately. A condition and the triage of
    its failure have to agree about which fields are in play, and when they are
    assembled in two places they drift: the triage then blames a field the
    condition never pinned and aborts a valid lease holder.

    ``upsert`` rebuilds the row from a read and puts it back, so it REWRITES
    every field it means only to preserve — and a rewrite from a snapshot taken
    before a concurrent writer DELETES whatever that writer landed.

    No amount of care in the caller closes that. Three attempts tried, and each
    was a guard computed from the same stale snapshot: by the time the snapshot
    is examined it can already be wrong, whatever it is examined FOR. So the
    write carries the proof instead — it lands only while the row still holds
    what this read saw, and a refusal sends the caller back to re-read.

    ``bedrock_gate_superseded_hashes`` is pinned transitively by the pointer, and
    the argument is NOT "the swap always mints a fresh hash" (it does not —
    ``persist_gate_token_hash`` re-asserts an existing one). It is that the list
    has exactly ONE writer, ``swap_gate_token_hash``, enforced by
    ``_reject_gate_owned_fields``; every write it makes is itself conditioned on
    the pointer it read; and in every reachable call the hash it writes differs
    from that pinned value. So no reachable write changes the list while leaving
    the pointer where we saw it.

    A field the snapshot did NOT see is pinned as ``attribute_not_exists``, not
    skipped — otherwise a rival that CREATES it slips through. That branch is
    also every first provision (no lease, no gate hash yet) and every
    non-Bedrock owner (no gate hash ever), so getting it wrong wedges them all.
    """
    names: dict[str, str] = {}
    values: dict = {}
    clauses: list[str] = []
    lease_expected: dict = {}
    snapshot = existing or {}
    for i, field in enumerate(_UPSERT_CAS_FIELDS):
        alias = f"#c{i}"
        names[alias] = field
        seen = snapshot.get(field)
        if field in _RELEASE_OWNED_FIELDS:
            lease_expected[field] = (field in snapshot, seen)
        elif field in _LEASE_CAS_FIELDS:
            # Recorded whatever it is, INCLUDING None: this condition pins every
            # field unconditionally, and a None means it pinned it as absent.
            lease_expected[field] = seen
        if field not in snapshot or (field not in _RELEASE_OWNED_FIELDS and seen is None):
            clauses.append(f"attribute_not_exists({alias})")
        else:
            clauses.append(f"{alias} = :c{i}")
            values[f":c{i}"] = seen
    kwargs: dict = {
        "ConditionExpression": " AND ".join(clauses),
        "ExpressionAttributeNames": names,
        # DynamoDB will not say WHICH clause failed, but it hands back the row
        # as it stood before the refused write — which is enough to tell a
        # benign rival mint from a lease takeover. See _raise_if_superseded.
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    return kwargs, lease_expected


def _deserialize_item(raw_item: dict | None) -> dict:
    """The raw (low-level, AttributeValue-typed) ``Item`` DynamoDB returns on a
    ``ReturnValuesOnConditionCheckFailure``, as a plain dict. Never raises: an
    unparseable item degrades to ``{}``, which every caller reads as "the
    pinned state is not what we expected" — the safe direction."""
    try:
        return {k: _DESERIALIZER.deserialize(v) for k, v in (raw_item or {}).items()}
    except Exception:
        logger.warning("failed to parse a conditional-check-failure item — treating it as unrecognisable")
        return {}


def _raise_if_superseded(owner_id: str, expected: dict, current: dict, *, where: str) -> None:
    """Refusal triage: retryable conflict, or ``ProvisionSuperseded``?

    A refused CAS means SOMETHING pinned moved, and the right response depends
    entirely on which — the two are opposites:

    - **Gate pointer moved.** A rival mint. Benign: rebuilding against the newer
      row and writing is exactly what should happen, and it is the whole point
      of the retry.
    - **Lease moved.** This caller has been SUPERSEDED. Rebuilding here would
      re-read the successor's claim and write it back out under the old
      holder's own execution — converting a correctly failed lease check into a
      successful write by the loser, which then returns and goes on to create
      an EFS access point and register a task definition alongside the winner.
      That is the double-provision hazard the lease exists to prevent, arriving
      through the fence meant to close it. So: stop, before the next side
      effect. Exactly what ``update_container_leased`` does at every checkpoint;
      this makes the conditional writes here honour the same contract.
    - **Release metadata moved.** A release transaction owns the row. Exact
      presence/value comparison makes present NULL distinct from absent.

    Both moved → superseded wins; the successor's mint is not ours to merge.

    ``expected`` IS THE PINNED SET, and only it is compared. Release fields use
    ``(present, value)`` tuples; lease fields retain their legacy value shape,
    where ``None`` means absent. Comparing a field the condition did NOT pin is a bug in the
    lockout direction — ``swap_gate_token_hash`` pins the attempt token only
    when the caller supplied one, and a deliberate takeover mints its claim with
    none, so blaming the attempt token there aborts a valid lease holder on a
    refusal that was only ever about the hash.

    Which is why neither call site builds this by hand: ``upsert`` gets it back
    from ``_unchanged_since_read`` (the same function that writes the condition)
    and the swap builds it in the same block that appends its lease clauses.

    A row deleted out from under us (or an item that would not parse) arrives as
    an empty ``current``, so a caller that pinned a lease stops — the safe
    direction, since the alternative is writing over an owner we cannot see.
    """
    for field, want in expected.items():
        actual = (field in current, current.get(field)) if field in _RELEASE_OWNED_FIELDS else current.get(field)
        if want != actual:
            raise ProvisionSuperseded(
                f"owner mutation state for {owner_id} moved during {where} ({field}) — "
                "a newer writer owns the row; this one must write nothing further"
            )


async def upsert(owner_id: str, fields: dict) -> dict:
    """Create or update a container record. Preserves id and created_at on existing items.

    Still a whole-item put, deliberately — unlike ``update_fields``, which became
    a scoped write. Its callers rely on the REPLACE: a reprovision through
    ``create_user_service`` is meant to clear ``access_point_id``,
    ``last_error``, ``deploy_claims`` and the rest of the previous incarnation's
    state.

    Also preserves everything in ``_UPSERT_PRESERVED_FIELDS`` that the caller
    did not pass, because this put_item REPLACES the whole item and
    create_user_service upserts mid-provision:

    - the FULL provision lease (``provision_claim_id`` +
      ``provision_claimed_at`` + ``provision_attempt_token`` +
      ``provision_execution_name``) — dropping it would make the in-flight
      provision look stale/unowned and re-claimable while it is still running.
      The SFN fields matter just as much as the claim: dropping
      ``provision_execution_name`` blinds Branch-A redispatch to the live
      execution, and dropping ``provision_attempt_token`` makes every
      attempt-token fence permissive (they all allow
      ``attribute_not_exists``), letting a superseded straggler pass its next
      checkpoint.
    - ``bedrock_gate_superseded_hashes`` and ``bedrock_gate_token_hash`` —
      dropping either orphans gate-token rows permanently (see the constant's
      comment). No caller MAY pass them — ``_reject_gate_owned_fields`` refuses.

    Preserving is not enough on its own: writing back a value another writer
    moved between this read and this put is a LOST UPDATE, not a preserve. So
    the put is conditional on the fields in ``_UPSERT_CAS_FIELDS`` still holding
    what the read saw. A refusal caused by a rival MINT rebuilds against the
    newer row; one caused by a lease TAKEOVER raises ``ProvisionSuperseded``
    and the caller must stop before its next side effect — see
    ``_raise_if_superseded`` for why those must not be treated alike.
    """
    _reject_gate_owned_fields("upsert", fields)
    _reject_release_owned_fields("upsert", fields)
    table = _get_table()
    for attempt in range(_UPSERT_CAS_ATTEMPTS):
        # CONSISTENT: this read is the ONLY source of the fields the whole-item
        # put below must carry over, and the lease ones were written moments
        # earlier by try_claim_provision / takeover_provision on this same
        # provision. A replica that has not caught up reports them absent and
        # the put then DROPS them — making an in-flight provision look unowned
        # and re-claimable, or minting a second id/created_at for a row that
        # already had them. One strongly consistent GetItem per provision; the
        # ~3s /container/status poll keeps reading the eventually-consistent
        # default.
        existing = await get_by_owner_id(owner_id, consistent=True)
        if existing and "release_update_id" in existing:
            raise ProvisionSuperseded(f"release transaction for {owner_id} owns the row during upsert")

        now = utc_now_iso()
        item = {
            "owner_id": owner_id,
            "id": (existing or {}).get("id") or str(uuid.uuid4()),
            "created_at": existing["created_at"] if existing else now,
            "updated_at": now,
            **fields,
        }
        for lease_field in _UPSERT_PRESERVED_FIELDS:
            if existing and lease_field in existing and lease_field not in fields:
                item[lease_field] = existing[lease_field]

        # Ensure owner_id is not overridden by fields
        item["owner_id"] = owner_id

        put_kwargs, lease_expected = _unchanged_since_read(existing)
        try:
            await run_in_thread(table.put_item, Item=item, **put_kwargs)
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            # Raises ProvisionSuperseded when it was the LEASE that moved: that
            # refusal is not a conflict to merge, it is a stop signal. The
            # pinned set comes from the builder above, never rebuilt here.
            _raise_if_superseded(owner_id, lease_expected, _deserialize_item(e.response.get("Item")), where="upsert")
            if attempt + 1 == _UPSERT_CAS_ATTEMPTS:
                raise
            continue  # a rival mint landed inside the window — rebuild on it
        break
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

    Claimable: no row, ``error``, ``stopped``, a ``running`` row explicitly
    marked ``needs_provision``, or a ``provisioning`` row whose lease is stale
    (``provision_claimed_at`` absent — legacy/wedged rows — or older than
    PROVISION_CLAIM_STALE_S with no checkpoint refresh). NOT claimable: an
    ordinary ``running`` row or a fresh ``provisioning`` lease. Release is by
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
        ":running": "running",
        ":true": True,
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
                "(attribute_not_exists(owner_id) OR #s = :error OR #s = :stopped "
                "OR (#s = :running AND needs_provision = :true) "
                "OR (#s = :prov AND (attribute_not_exists(provision_claimed_at) OR provision_claimed_at < :stale))) "
                "AND attribute_not_exists(release_update_id)"
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
    an in-process bucket (the pattern the old agent-mail reply relay used for burst
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


async def try_claim_daily_system_notice(owner_id: str, *, day: str, kind: str = "complaint") -> bool:
    """Claim the once-per-UTC-day slot for a ``kind`` platform notice to ``owner_id``.

    ``try_consume_daily_email_send``'s shape minus the counter: one notice per
    day, not N. A single conditional write flips ``system_notice_day_<kind>`` to
    ``day``, admitted only when the stored value is absent or from another day —
    so a burst of events (SES fans one complaint out per recipient address)
    collapses to a single notice. DURABLE for the same reason as its sibling:
    several backend tasks run behind one ALB, so an in-process guard would give
    a real limit of one-per-task and reset on every deploy.

    These notices ride ``send_to_owner(system_notice=True)``, which deliberately
    BYPASSES the daily send cap — this is the only bound on them.

    **Fails OPEN**, like its sibling: a DynamoDB fault returns True rather than
    swallowing a notice the owner needs (their agents just lost the ability to
    send mail). Worst case is a duplicate email. The ``attribute_exists`` guard
    means an owner with no container row never claims, which is unreachable in
    practice — provisioned agent addresses imply a container row.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #d = :d",
            ConditionExpression="attribute_exists(owner_id) AND (attribute_not_exists(#d) OR #d <> :d)",
            ExpressionAttributeNames={"#d": f"system_notice_day_{kind}"},
            ExpressionAttributeValues={":d": day},
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        return True  # fail open — see docstring


# Reserved owner_id prefix for pass-admission rows (see try_claim_pass).
PASS_KEY_PREFIX = "__pass__#"


async def try_claim_pass(name: str, *, interval_s: int, now: int | None = None) -> bool:
    """Admit at most one instance into periodic pass ``name`` per ``interval_s`` window.

    ONE conditional write on a fixed-key row: set ``last_run_at`` only when it is
    absent or at/older than ``now - interval_s`` (``<=``, not ``<`` -- with a
    strict less-than, a pass whose interval equals its caller's tick, e.g. the
    60 s worker passes and sweepers, would never see ``elapsed == interval_s``
    admitted and would only fire every other tick). Modelled on
    ``try_claim_daily_system_notice`` and adds no new pattern.

    This is PASS ADMISSION, not a lease. It admits a *scan*; it holds custody of
    nothing. Every per-row claim inside every pass stays the correctness
    boundary, so a wrong, stale or unreachable row degrades to exactly what the
    code did before this existed: a duplicate scan.

    Cadence lives on the row, not in a process frame, so ``interval_s`` -- not
    process uptime -- decides a daily purge and a deploy no longer forces one.

    FAILS CLOSED, unlike ``try_claim_daily_system_notice``: ANY fault (a
    conditional refusal, a ``ClientError``, or a ``BotoCoreError`` -- endpoint/
    connect/read timeouts, ``NoCredentialsError``, none of which carry an
    ``Error`` code so ``ClientError`` alone would miss them) skips this tick
    rather than admitting every instance. A skipped 60 s sweep costs a minute
    of latency on a trial park or a swept run; a fleet-wide fail-open costs
    N x every scan in the product exactly when DynamoDB is already unhappy.
    ``tests/unit/repositories/test_container_repo_pass_admission.py`` asserts
    the difference so nobody "fixes" it.

    ponytail: one reserved-key row per pass name, all on the containers table.
    Fine at ~6 writes/min fleet-wide; give it its own table only if that stops
    being true.
    """
    stamp = int(time.time()) if now is None else int(now)
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{PASS_KEY_PREFIX}{name}"},
            UpdateExpression="SET last_run_at = :now",
            ConditionExpression="attribute_not_exists(last_run_at) OR last_run_at <= :cutoff",
            ExpressionAttributeValues={":now": stamp, ":cutoff": stamp - int(interval_s)},
        )
        return True
    except ClientError as e:
        # Conditional refusal (contention -- another instance already won this
        # window) fails closed same as any other fault, but is NOT itself a
        # fault: the callers' own metric already reports it as "lost". Any
        # other ClientError (throttling, a real DynamoDB error) is a fault the
        # callers' boolean-only metric can't see, so log + emit it here.
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("pass admission fault name=%s: %s", name, e)
            put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "fault"})
        return False
    except Exception as e:
        # BotoCoreError (endpoint/connect/read timeouts, NoCredentialsError, ...)
        # carries no Error code, so it can't be ClientError -- always a fault.
        logger.warning("pass admission fault name=%s: %s", name, e)
        put_metric("lifecycle.pass.claimed", dimensions={"pass": name, "outcome": "fault"})
        return False


# How long an owner-lifecycle claim stays exclusive before another caller may take it over.
# It fences the OAuth connect / disconnect critical section in routers/connections.py: a
# Secrets-Manager write plus the Hermes profile fan-out, tens of seconds at worst. 120 is a
# ceiling, not a measurement -- the failure mode of a too-SHORT lease is two writers on one
# owner's credentials, and of a too-long one is the owner waiting out a 409.
LIFECYCLE_CLAIM_STALE_S = 120

# Reserved-key row per owner, like PASS_KEY_PREFIX above: the guard must work for an owner who
# has NO containers row yet (a connect can precede provisioning), so it cannot ride the owner's
# own row the way the deploy claim does.
LIFECYCLE_KEY_PREFIX = "__lifecycle__#"


async def try_claim_owner_lifecycle(owner_id: str) -> str | None:
    """Take the owner's exclusive lifecycle lease, or ``None`` if someone else holds it.

    Replaces the EFS file lock ``owner_lifecycle`` used to take: the backend runs 2-4 tasks and
    its shared mount is gone, so a filesystem lock now succeeds independently in every task and
    fences nothing. One conditional write is the only thing that still serializes them.

    FAILS CLOSED -- any fault refuses, rather than admitting every task into a section that
    writes an owner's credentials. A refused connect is a 409 the caller retries; two admitted
    connects interleave a Secrets-Manager write with a disconnect's teardown.

    Returns a fencing token: a caller that hung past ``LIFECYCLE_CLAIM_STALE_S`` was taken over,
    and an unconditional release would delete its successor's live claim.
    """
    now = int(time.time())
    token = uuid.uuid4().hex
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{LIFECYCLE_KEY_PREFIX}{owner_id}"},
            UpdateExpression="SET claimed_at = :now, tok = :tok",
            ConditionExpression="attribute_not_exists(claimed_at) OR claimed_at < :cutoff",
            ExpressionAttributeValues={
                ":now": now,
                ":tok": token,
                ":cutoff": now - LIFECYCLE_CLAIM_STALE_S,
            },
        )
        return token
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning("try_claim_owner_lifecycle: DDB fault for owner=%s: %s", owner_id, e)
        return None
    except Exception as e:  # noqa: BLE001 -- BotoCoreError carries no Error code; fail closed
        logger.warning("try_claim_owner_lifecycle: fault for owner=%s: %s", owner_id, e)
        return None


async def release_owner_lifecycle_claim(owner_id: str, *, token: str) -> None:
    """Drop the owner's lifecycle claim, but only if ``token`` still holds it.

    Never raises: it runs in the guard's ``finally``, where an exception would mask the real
    error, and a lost release self-expires after ``LIFECYCLE_CLAIM_STALE_S``.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{LIFECYCLE_KEY_PREFIX}{owner_id}"},
            UpdateExpression="REMOVE claimed_at, tok",
            ConditionExpression="tok = :tok",
            ExpressionAttributeValues={":tok": token},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            logger.warning(
                "release_owner_lifecycle_claim: failed for owner=%s -- claim expires in %ss",
                owner_id,
                LIFECYCLE_CLAIM_STALE_S,
            )
    except Exception as e:  # noqa: BLE001 -- never raises; see docstring
        logger.warning("release_owner_lifecycle_claim: fault (%s) for owner=%s", e, owner_id)


# How long a marketplace-deploy claim stays exclusive before another deploy may
# take it over. Was 900 (15m) when the claim was keyed per-listing — sized for
# a single lease. Now the claim is OWNER-WIDE (see try_claim_owner_deploy), so
# a wedged claim blocks every hire the owner tries, not just one listing's.
#
# **The lease MUST outlive the deploy it fences.** It briefly did not: a 300s
# lease was sized against a measured ~85s deploy, then the gateway pairing RPC
# timeout was raised to 90s, putting the worst-case pairing above the lease. A
# lease that expires mid-deploy is worse than no lease: the second hire takes it
# over and runs concurrently, the first hire's ``finally`` release is correctly
# no-op'd by the fencing token, and nothing logs the overlap.
#
# 720 is kept at its ECS-era value. The pairing RPCs it was sized against
# (``pair_with_openclaw``, ~546s worst case) are gone with the OpenClaw gateway,
# so what the lease now fences is strictly shorter: the S3 artifact GET, the
# profile write, the Paperclip mint, routine recreation and the integration
# reconcile. It stays a generous ceiling rather than being re-derived downward —
# the failure mode of a too-SHORT lease is a concurrent-write incident, and of a
# too-long one is a wedged claim the owner waits out.
DEPLOY_CLAIM_STALE_S = 720

# Fixed map key for the owner-wide deploy claim. The listing that triggered a
# claim is still recorded in the claim VALUE (for diagnostics) but no longer
# part of the key — see try_claim_owner_deploy for why.
_DEPLOY_CLAIM_KEY = "__deploy__"


async def try_claim_owner_deploy(
    owner_id: str,
    *,
    listing_id: str,
    fail_open: bool = True,
) -> tuple[str | None, str | None]:
    """Take an exclusive lease on "this owner has an agent install in flight".

    FENCED — every path that installs a marketplace/built agent WORKSPACE into
    the owner's running container takes this claim: the buyer-facing hire
    (``routers/marketplace_listings.deploy_listing``), the Friday-asks /
    owner-approves hire (``marketplace_hire.deploy_for_approval``, reached from
    the Teams approvals route — the "Needs Attention" band lists several at once,
    so approving three in a row is the same three-in-nine-seconds shape as the
    incident), and Atlas registering an agent it built
    (``routers/agent_builder.register_built_agent``).

    ``marketplace_hire.create_hire_approval`` also takes this claim before its
    Paperclip pre-create and approval reconciliation. It installs no workspace,
    but must serialize with those paths so concurrent recommendations cannot
    file duplicate approvals for one logical hire.

    ``company_deploy_service.run_company_deploy`` is fenced too, but its wall
    clock is N x a single deploy's — longer than one ``DEPLOY_CLAIM_STALE_S``
    lease. It holds the claim across the whole run by calling
    ``extend_owner_deploy_claim`` at each per-agent step, so the lease only
    goes stale if the process actually dies mid-run (the takeover posture a
    single-deploy timeout gets).

    Also NOT fenced, and out of scope for this claim: the two BARE agent creates
    (``routers/teams/agents.create_agent`` and ``routers/websocket_chat``), which
    drive the same ``agents.create``/``agents.update`` pair against the same
    container but install no workspace, no config merge and no artifact. The
    lists above are the workspace-installing paths only — not an exhaustive
    accounting of everything that talks to the container.

    Returns ``(token, held_listing_id)``:

    - On success: ``(token, None)``. Pass ``token`` back to
      ``release_owner_deploy_claim`` — an unconditional release would let a
      caller that hung past ``DEPLOY_CLAIM_STALE_S`` (and was therefore taken
      over) delete the live claim of whoever succeeded it, reopening the exact
      overlap window this exists to close. Same fencing discipline as
      ``try_claim_provision``'s ``provision_claim_id``.
    - On refusal by a live claim: ``(None, held_listing_id)`` — the
      ``listing_id`` of whoever currently holds it, read off the SAME
      conditional write via ``ReturnValuesOnConditionCheckFailure="ALL_OLD"``
      (no extra read, no race against a follow-up ``get_item``). The caller
      needs this: "you already have THIS listing installing" (don't retry —
      retrying is the Pulse-2 duplicate-mint bug) and "a DIFFERENT listing of
      yours is installing" (retry after a beat) require OPPOSITE client
      advice, and the claim being owner-wide means the caller can no longer
      tell them apart just from getting refused.
    - On refusal because the owner has no container row yet, or if the held
      claim's listing_id can't be parsed for any reason: ``(None, None)``.

    OWNER-WIDE, not per-listing: this used to key the claim on ``listing_id``
    (``SET deploy_claims.#lid = :claim`` with ``#lid`` bound to the listing),
    which only ever fenced two deploys of the SAME listing. Three different
    listings hired within seconds of each other wrote three different map
    keys, so all three conditions passed and all three deploys ran
    concurrently against the one OpenClaw container — two came back 500 and
    rolled back. The container already serializes config mutations internally
    (a ``KeyedAsyncQueue`` wrapped in ``withFileLock``), so concurrent callers
    only bought themselves queueing + retry churn, never real parallelism:
    ``agents.create`` measured 4s idle vs. 39s/51s/31s under 3-way concurrency.
    Serializing per owner costs nothing and closes that hole; the map key is
    now the constant ``_DEPLOY_CLAIM_KEY`` and ``listing_id`` is kept only in
    the claim value for diagnostics.

    Why durable, and why here: the ``.template``-sidecar check in
    ``routers/marketplace_listings`` is a read, not a lock — two OVERLAPPING
    deploys can both see "not installed" (the second starts before the first has
    extracted anything) and both mint an agent. That is how prod grew "Pulse 2":
    the second deploy started 35s before the first wrote its sidecar, back when
    the sidecar landed at the END of the deploy. The backend runs several ECS
    tasks behind an ALB (the two prod deploys landed on different ones), so an
    in-process, per-instance cache cannot fence this — it has to be a
    conditional write. (``core/services/idempotency.py`` was exactly such a
    cache, kept for admin write handlers; sub-project C deleted it for this
    same reason -- see ``webhook_dedup.idempotency()``.) It rides the existing
    per-owner ``containers`` row rather
    than a new table, matching ``try_claim_provision`` /
    ``try_consume_daily_email_send`` on the same row.

    Two writes: the first materializes the ``deploy_claims`` map (DynamoDB
    rejects a nested SET when the parent path is absent) and is unconditional +
    idempotent; the second is the actual claim, admitted only when no live claim
    exists for this owner. Each claim is stored as
    ``{"claimed_at": epoch, "tok": …, "listing_id": …}`` — ``claimed_at`` drives
    takeover of an abandoned claim, ``tok`` fences the release. NOT ``at``: that
    is a reserved DynamoDB keyword, and an unaliased ``deploy_claims.#lid.at``
    in the condition is a ValidationException, which the fail-open below would
    have quietly turned into "no fence at all".

    **Never raises.** Workspace installs use the default ``fail_open=True``:
    any fault admits the deploy with a token that fences nothing. The brief
    approval-precreation critical section passes ``fail_open=False`` because a
    duplicate Paperclip approval is durable user-visible state and the caller
    can safely degrade to no recommendation until DynamoDB recovers. The handler
    is deliberately ``Exception``-wide so ``BotoCoreError`` subclasses without
    an AWS error code cannot escape either policy.
    """
    table = _get_table()
    now = int(time.time())
    token = uuid.uuid4().hex
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET deploy_claims = if_not_exists(deploy_claims, :empty)",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":empty": {}},
        )
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET deploy_claims.#lid = :claim",
            ConditionExpression=("attribute_not_exists(deploy_claims.#lid) OR deploy_claims.#lid.claimed_at < :cutoff"),
            ExpressionAttributeNames={"#lid": _DEPLOY_CLAIM_KEY},
            ExpressionAttributeValues={
                ":claim": {"claimed_at": now, "tok": token, "listing_id": listing_id},
                ":cutoff": now - DEPLOY_CLAIM_STALE_S,
            },
            # On refusal, hand back the row as it stood BEFORE this write so we
            # can read who holds the claim without a second (racy) get_item.
            # The condition can only fail when deploy_claims.#lid already
            # exists (the other half of the OR), so the held claim is always
            # present in the returned item on this specific failure.
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        return token, None
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None, _held_listing_id(e.response.get("Item"))
        # ERROR, with the code: fail-open makes a MALFORMED expression look
        # exactly like a transient fault, and a ValidationException here means
        # the fence is off for every deploy until someone notices.
        logger.error(
            "try_claim_owner_deploy: DDB fault (%s: %s) for owner=%s listing=%s — %s",
            e.response["Error"].get("Code"),
            e.response["Error"].get("Message"),
            owner_id,
            listing_id,
            "admitting UNFENCED" if fail_open else "refusing while the claim is unprovable",
        )
        return (token, None) if fail_open else (None, None)
    except Exception as e:  # noqa: BLE001 — fail open on ANY fault; see docstring
        logger.error(
            "try_claim_owner_deploy: fault (%s: %s) for owner=%s listing=%s — %s",
            type(e).__name__,
            e,
            owner_id,
            listing_id,
            "admitting UNFENCED" if fail_open else "refusing while the claim is unprovable",
        )
        return (token, None) if fail_open else (None, None)


def _held_listing_id(raw_item: dict | None) -> str | None:
    """Best-effort extraction of ``deploy_claims.__deploy__.listing_id`` from
    the raw (low-level, AttributeValue-typed) ``Item`` DynamoDB returns on a
    ``ReturnValuesOnConditionCheckFailure`` conditional-write failure.

    Never raises: a malformed/missing item just means the caller falls back
    to the conservative "treat as same listing" branch (see
    ``routers/marketplace_listings``), not a crash.
    """
    if not raw_item:
        return None
    try:
        item = {k: _DESERIALIZER.deserialize(v) for k, v in raw_item.items()}
        return (item.get("deploy_claims") or {}).get(_DEPLOY_CLAIM_KEY, {}).get("listing_id")
    except Exception:
        logger.warning("_held_listing_id: failed to parse ALL_OLD item — falling back to unknown holder")
        return None


async def set_deployed_company(owner_id: str, *, listing_id: str, name: str) -> None:
    """Record that this owner's workspace holds a deployed company package.

    Written by ``run_company_deploy`` on REAL success only (agents were
    created and materialized — never on the creating-nothing early returns),
    and read by the deploy route to enforce ONE company package per workspace:
    a second company deploy — same listing or a different one — is refused
    409, because a whole second org grafted onto the same Friday duplicates
    role structures.

    A workspace wipe/reprovision deletes the row, so a fresh slate can deploy
    again — that is the intended semantics, not a leak.

    **Never raises**: the deploy has already succeeded when this runs; a
    marker miss just means the fence is open for this owner (logged loudly).
    """
    try:
        await update_fields(
            owner_id,
            {
                "deployed_company": {
                    "listing_id": listing_id,
                    "name": name,
                    "deployed_at": int(time.time()),
                }
            },
        )
    except Exception as e:  # noqa: BLE001 — see docstring
        logger.error(
            "set_deployed_company: marker write failed for owner=%s listing=%s (%s: %s) "
            "— one-company fence is OPEN for this owner",
            owner_id,
            listing_id,
            type(e).__name__,
            e,
        )


async def extend_owner_deploy_claim(owner_id: str, *, token: str) -> None:
    """Refresh the claim's ``claimed_at`` so a long-running holder keeps its
    lease. Company deploys call this once per agent step: each step is bounded
    well under ``DEPLOY_CLAIM_STALE_S``, so a live run never goes stale, while
    a dead one stops extending and is taken over on the normal cutoff.

    Conditioned on ``tok`` — a holder that already lost its lease to takeover
    cannot resurrect it. **Never raises**: a failed extension just means the
    lease can go stale mid-run, which is the pre-existing takeover posture.
    """
    table = _get_table()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET deploy_claims.#lid.claimed_at = :now",
            ConditionExpression="deploy_claims.#lid.tok = :tok",
            ExpressionAttributeNames={"#lid": _DEPLOY_CLAIM_KEY},
            ExpressionAttributeValues={":now": int(time.time()), ":tok": token},
        )
    except Exception as e:  # noqa: BLE001 — see docstring
        logger.warning(
            "extend_owner_deploy_claim: could not extend for owner=%s (%s: %s) — lease may go stale",
            owner_id,
            type(e).__name__,
            e,
        )


def _has_timeout_cause(exc: BaseException | None) -> bool:
    """True when ``exc`` is a ``TimeoutError`` or was raised ``from`` one.

    Deploy callers wrap the failure before it reaches the claim release — the
    marketplace router re-raises as ``HTTPException(500) from exc``, the
    agent-builder as a 502 — so the timeout that matters usually sits one link
    down the chain. Only ``__cause__`` is followed, never ``__context__``:
    implicit chaining would pick up an unrelated exception that merely happened
    to be in flight and wedge the owner's claim for nothing. Depth-capped so a
    ``__cause__`` cycle cannot hang the ``finally`` this runs in.
    """
    for _ in range(10):
        if exc is None:
            return False
        if isinstance(exc, TimeoutError):
            return True
        exc = exc.__cause__
    return False


async def release_owner_deploy_claim(
    owner_id: str, *, listing_id: str, token: str, cause: BaseException | None = None
) -> None:
    """Drop the owner's deploy claim, but only if ``token`` still holds it.

    ``listing_id`` is accepted for logging symmetry with the claim call and
    isn't part of the key — see ``try_claim_owner_deploy``.

    ``cause`` — the exception the deploy is unwinding with, if any. **A
    ``TimeoutError`` cause KEEPS the claim**, deliberately, and this is the
    other half of ``catalog_service.unwind_partial_deploy``'s timeout
    carve-out. That carve-out leaves the container-side install untouched
    precisely because the container may still be mutating it; releasing the
    claim in the same breath would hand the next hire a green light to run a
    second install concurrently with the still-active first one — the exact
    overlap the claim exists to prevent, reintroduced on the one path that
    needs it most. So the timed-out deploy holds its claim and lets the LEASE
    be the settling period: nothing else touches this owner's container until
    ``DEPLOY_CLAIM_STALE_S`` (600s) has elapsed since the claim was taken.

    That is a real bound, not a hope. The step that used to raise a bare
    ``TimeoutError`` was the gateway pairing RPC pair, which is gone with the
    OpenClaw runtime; every step a deploy has left swallows its own errors or
    raises ``httpx.TimeoutException`` (NOT a builtin ``TimeoutError``), and the
    whole sequence under the claim — the S3 artifact GET, the ~15s-timeout
    Paperclip mint, the profile write — is tens of seconds against a 720s lease.
    The sidecar cross-check in ``_live_install_of`` is the belt to this brace: if
    the deploy did land, the next hire resolves to it as ``already_installed``
    rather than minting a duplicate.

    The condition on the write is the point: a deploy that hung past
    ``DEPLOY_CLAIM_STALE_S`` was taken over, and an unconditional REMOVE here
    would delete its successor's live claim — letting a third caller in and
    reopening the overlap window. A ConditionalCheckFailed means "not ours
    anymore", which is a no-op, not an error.

    Best-effort otherwise: a lost release self-expires after
    ``DEPLOY_CLAIM_STALE_S``, so a fault delays a retry rather than wedging the
    owner's next hire. Never raises — it runs in the deploy's ``finally``,
    where an exception would mask the real deploy error — and that has to hold
    for ``BotoCoreError`` (no ``Error`` code, so ``ClientError`` alone misses
    it) as much as for a ``ClientError``.
    """
    if _has_timeout_cause(cause):
        logger.warning(
            "release_owner_deploy_claim: HOLDING the claim for owner=%s listing=%s — the deploy failed with a "
            "client-side timeout, so the container may still be writing; the lease settles it in %ss",
            owner_id,
            listing_id,
            DEPLOY_CLAIM_STALE_S,
        )
        return
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="REMOVE deploy_claims.#lid",
            ConditionExpression="deploy_claims.#lid.tok = :tok",
            ExpressionAttributeNames={"#lid": _DEPLOY_CLAIM_KEY},
            ExpressionAttributeValues={":tok": token},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "release_owner_deploy_claim: claim for owner=%s listing=%s is no longer ours — leaving it",
                owner_id,
                listing_id,
            )
            return
        logger.warning(
            "release_owner_deploy_claim: failed for owner=%s listing=%s — claim expires in %ss",
            owner_id,
            listing_id,
            DEPLOY_CLAIM_STALE_S,
        )
    except Exception as e:  # noqa: BLE001 — never raises; see docstring
        logger.warning(
            "release_owner_deploy_claim: fault (%s: %s) for owner=%s listing=%s — claim expires in %ss",
            type(e).__name__,
            e,
            owner_id,
            listing_id,
            DEPLOY_CLAIM_STALE_S,
        )


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
            ConditionExpression="provision_claim_id = :cid AND attribute_not_exists(release_update_id)",
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
    """Take over an unreserved provision lease — deliberate doors only
    (trial->paid conversion, /container/recover, admin force-apply, provider
    change, debug). Returns the new fencing token.

    Never blocked by another provision: a deliberate reprovision must win,
    except while a release transaction owns the row. Any in-flight
    poll-driven provision is fenced out at its next leased checkpoint (its
    claim_id no longer matches). Only the LEASE fields are stamped — the row's
    ``status`` is preserved (created as "provisioning" if the row is new), so a
    takeover on a running container does not flip the UI into the provisioning
    stepper before the reprovision itself decides what to write.
    """
    table = _get_table()
    claim_id = str(uuid.uuid4())
    now_iso = utc_now_iso()
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET provision_claim_id = :cid, provision_attempt_token = :cid, provision_claimed_at = :now, "
                "updated_at = :iso, #s = if_not_exists(#s, :prov), "
                "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
            ),
            ConditionExpression="attribute_not_exists(release_update_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":cid": claim_id,
                ":now": int(time.time()),
                ":iso": now_iso,
                ":prov": "provisioning",
                ":newid": str(uuid.uuid4()),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ProvisionSuperseded(f"release transaction for {owner_id} owns the row") from e
        raise
    return claim_id


async def update_container_leased(
    owner_id: str,
    claim_id: str,
    fields: dict | None = None,
    *,
    generation: str | None = None,
    attempt_token: str | None = None,
    release_lease: bool = False,
    expected: dict | None = None,
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
    - ``expected`` -- when given, additionally requires each named field to
      still hold the given value. For a write that CLEARS a field a concurrent
      run might have already moved on from (e.g. the CREATE_FAILED ARN clear),
      this stops a stale writer from wiping out a value a newer attempt has
      since written — raises ``ProvisionSuperseded`` the same as any other
      fence miss.

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
    # The third caller-supplied `fields` door. Its lease condition says nothing
    # about the gate pointer, so a caller naming the superseded list here would
    # replace it with no fence at all — the same shape _GATE_OWNED_FIELDS closes
    # on the other two.
    _reject_gate_owned_fields("update_container_leased", fields or {})
    _reject_release_owned_fields("update_container_leased", fields or {})
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
    condition = "provision_claim_id = :cid AND attribute_not_exists(release_update_id)"
    values[":cid"] = claim_id
    if generation is not None:
        condition += " AND (attribute_not_exists(provision_generation) OR provision_generation = :gen)"
        values[":gen"] = generation
    if attempt_token is not None:
        condition += " AND (attribute_not_exists(provision_attempt_token) OR provision_attempt_token = :atok)"
        values[":atok"] = attempt_token
    for i, (key, value) in enumerate((expected or {}).items()):
        alias = f"#e{i}"
        names[alias] = key
        if value is None:
            # "no value yet" must match a field that was NEVER written (absent), not only one
            # explicitly cleared to null -- DynamoDB's `=` against a NULL-typed value is false
            # for an absent attribute (a bare equality here would raise ProvisionSuperseded on
            # every fresh row's first write). Mirrors the generation/attempt_token OR-absent
            # shape above.
            condition += f" AND (attribute_not_exists({alias}) OR {alias} = :ev{i})"
        else:
            condition += f" AND {alias} = :ev{i}"
        values[f":ev{i}"] = value
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
    synchronous provision failure funnels here; the scattered pre-emptive
    ``status="error"`` writes the ECS provisioner used to fire first — which no-op'd this
    release's ``status = provisioning`` condition, dropping the cause — were removed, and
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
    condition = "provision_claim_id = :cid AND #s = :prov AND attribute_not_exists(release_update_id)"
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
_CONVERSION_MARKER_FIELDS = frozenset(
    {"conversion_pending", "substatus", "conversion_started_at", "conversion_deferred_reason"}
)


async def update_conversion_fields(owner_id: str, fields: dict) -> None:
    """Scoped, lease-safe write of the trial->paid conversion marker fields
    onto an EXISTING containers row.

    THE ALLOWLIST is what this adds over a bare ``update_fields`` (itself now a
    scoped write): it pins the write to the marker fields FOREVER, so no future
    caller can quietly grow this into a status/lease writer.

    That distinction was a live incident, back when ``update_fields`` was a
    get→put of the whole item: the conversion's release write
    (``run_conversion_reprovision``) read the row BEFORE its own freshly
    dispatched provision won ``try_claim_provision``, then put the whole stale
    item back — resurrecting ``status="stopped"`` over the just-claimed
    "provisioning" AND erasing every lease field (``provision_claim_id`` /
    ``provision_attempt_token`` / ``provision_execution_name``). The
    already-started SFN runner then hit ``ProvisionSuperseded`` at its first
    checkpoint and stood down, leaving a PAYING owner stopped with nothing
    running and nothing scheduled. A scoped write on exactly the marker fields
    cannot touch ``status`` or the lease, so the race collapses to a cosmetic
    substatus overwrite at worst (the claim's "claimed" label may be nulled for
    one poll; the next checkpoint/status write refreshes it).

    ``fields`` may only contain conversion marker keys (allowlist above);
    anything else raises ``ValueError`` — this helper must never become a
    general row writer. The write itself is ``update_fields``, so the rest of
    the contract (``None`` -> NULL, missing row -> silent no-op, the
    ``updated_at`` stamp, the status event) is that function's, once, rather
    than a second copy of its expression builder drifting alongside it.
    """
    unknown = set(fields) - _CONVERSION_MARKER_FIELDS
    if unknown:
        raise ValueError(f"update_conversion_fields: disallowed fields {sorted(unknown)}")
    await update_fields(owner_id, fields)


async def clear_conversion_deferral(owner_id: str, *, deferred_reason: str) -> bool:
    """CAS: drop a DEFERRED trial->paid conversion's self-heal anchor
    (``conversion_started_at``) and its deferral marker, but ONLY while the row
    still carries the deferral the caller observed.

    The re-arm that calls this (``rearm_deferred_conversion``) reads the row
    eventually-consistently and then writes. A fresh dispatch
    (``_stamp_conversion_dispatch``) clears the marker on its way in, so a
    re-arm racing it — or one whose read returned the pre-stamp row — must
    refuse here rather than disarm the guard of a dispatch that may already be
    stopping + draining the container. Returns whether the write landed.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET conversion_started_at = :null, conversion_deferred_reason = :null, updated_at = :iso"
            ),
            ConditionExpression=(
                "attribute_exists(owner_id) AND conversion_pending = :pending AND conversion_deferred_reason = :seen"
            ),
            ExpressionAttributeValues={
                ":null": None,
                ":pending": True,
                ":seen": deferred_reason,
                ":iso": utc_now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    publish_status_changed(owner_id)
    return True


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
    Both clauses evaluate atomically at write time — no read-then-write
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
            ConditionExpression="#s = :stopped AND attribute_not_exists(provision_claim_id)",
            ExpressionAttributeNames={"#s": "status", "#rt": "retryable", "#le": "last_error"},
            ExpressionAttributeValues={
                ":err": "error",
                ":sub": "provision_failed",
                ":stopped": "stopped",
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
    release — ``services.provision_dispatch.dispatch_provision`` (the poll-driven path) and
    the deliberate-reprovision doors — and this repository already owns the
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
    - An AWS ``ClientError`` whose code is ``ValidationException`` is a
      malformed request — deterministic; other client errors (throttle,
      capacity, 5xx) keep the retryable default.
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
    # An AWS ValidationException is a malformed request (bad attribute value,
    # empty index key, ...) — identical on every retry, so deterministic.
    # Other ClientErrors (throttling, 5xx, capacity) keep the retryable
    # default below.
    if isinstance(exc, ClientError):
        code = (exc.response or {}).get("Error", {}).get("Code", "")
        if code == "ValidationException":
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


async def mark_needs_provision(owner_id: str) -> dict | None:
    """Block new turn ingress and stamp a generation for provider convergence.

    Return the prior state so a failed billing write can restore it only while
    this generation is still current. No-op when no container row exists.
    """
    generation = uuid.uuid4().hex
    try:
        response = await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET needs_provision = :yes, provider_switch_generation = :gen, updated_at = :now",
            ConditionExpression="attribute_exists(owner_id) AND attribute_not_exists(release_update_id) "
            "AND (attribute_not_exists(needs_provision) OR needs_provision = :no)",
            ExpressionAttributeValues={":yes": True, ":no": False, ":gen": generation, ":now": utc_now_iso()},
            ReturnValues="ALL_OLD",
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            if exc.response.get("Item") or await get_by_owner_id(owner_id, consistent=True):
                raise ProvisionSuperseded(f"owner mutation row for {owner_id} is reserved") from exc
            return None
        raise
    previous = response.get("Attributes") or {}
    publish_status_changed(owner_id)
    return {
        "generation": generation,
        "previous_needs_provision": previous.get("needs_provision", False),
        "previous_generation": previous.get("provider_switch_generation"),
    }


async def restore_needs_provision(owner_id: str, mark: dict) -> bool:
    """Undo only this switch's marker; a newer generation wins the race."""
    values = {":gen": mark["generation"], ":prior": bool(mark["previous_needs_provision"]), ":now": utc_now_iso()}
    update = "SET needs_provision = :prior, updated_at = :now"
    if mark.get("previous_generation") is None:
        update += " REMOVE provider_switch_generation"
    else:
        update += ", provider_switch_generation = :oldgen"
        values[":oldgen"] = mark["previous_generation"]
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=update,
            ConditionExpression="provider_switch_generation = :gen AND attribute_not_exists(release_update_id)",
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise
    publish_status_changed(owner_id)
    return True


async def update_status(owner_id: str, status: str, substatus: str | None = None) -> dict | None:
    fields = {"status": status, "substatus": substatus}
    return await update_fields(owner_id, fields)


async def update_fields(owner_id: str, fields: dict) -> dict | None:
    """Merge ``fields`` into an EXISTING row. Returns the updated item, ``None``
    when there is no row.

    A SCOPED ``update_item`` — it writes the caller's fields and nothing else,
    which is what the name always promised. It used to be a get→put of the whole
    item, the pattern half the scoped writers below warn against by name: every
    attribute the snapshot carried was written back, so a concurrent writer's
    ``status``, lease, ``stop_reason`` or gate-token pointer could be resurrected
    from a read milliseconds old — the documented double-provision hazard, on all
    ~20 callers. Nothing is read now; ``ALL_NEW`` returns the merged item.

    EVERY attribute name is aliased, uniformly. Not tidiness: ``status`` is a
    DynamoDB reserved word and the commonest field written here, an unaliased one
    is a ValidationException, and this codebase has shipped that as a silently
    dead fence before. A ``None`` value is written as NULL exactly as the get→put
    did, never REMOVE'd. A conditional refusal distinguishes a missing row from
    a row reserved by a release transaction using DynamoDB's refused-item return.
    """
    _reject_gate_owned_fields("update_fields", fields)
    _reject_release_owned_fields("update_fields", fields)
    names: dict[str, str] = {}
    set_parts: list[str] = []
    values: dict = {":iso": utc_now_iso()}
    for i, (key, value) in enumerate(fields.items()):
        alias = f"#f{i}"
        names[alias] = key
        set_parts.append(f"{alias} = :v{i}")
        values[f":v{i}"] = value
    set_parts.append("updated_at = :iso")
    kwargs: dict = {
        "Key": {"owner_id": owner_id},
        "UpdateExpression": "SET " + ", ".join(set_parts),
        "ConditionExpression": "attribute_exists(owner_id) AND attribute_not_exists(release_update_id)",
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
        "ReturnValuesOnConditionCheckFailure": "ALL_OLD",
    }
    if names:  # DDB rejects an empty map; an empty `fields` still stamps updated_at
        kwargs["ExpressionAttributeNames"] = names
    try:
        response = await run_in_thread(_get_table().update_item, **kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raw_item = e.response.get("Item")
            refused = _deserialize_item(raw_item)
            if raw_item:
                reason = "reserved by a release transaction" if "release_update_id" in refused else "not recognisable"
                raise ProvisionSuperseded(f"owner mutation row for {owner_id} is {reason}") from e
            return None  # no row — nothing to merge into
        raise
    publish_status_changed(owner_id)
    return response.get("Attributes")


async def ensure_owner_tokens(owner_id: str, *, hook_token: str, gateway_token: str) -> dict:
    """Set ``hook_token``/``gateway_token`` (and ``id``) only where the row has none; return the row.

    ONE ``if_not_exists`` UpdateItem, so concurrent provision runs each offering fresh
    tokens converge on the first writer's pair, and an owner that already has tokens keeps
    them (they are snapshotted into Paperclip adapters, so rotating one silently breaks
    every run). Callers use the RETURNED values, never their own candidates.

    Raises ``ProvisionSuperseded`` when there is no row: the claim that created it is gone,
    and an UpdateItem would otherwise create a token-only ghost row.
    """
    try:
        response = await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            # ``id`` too: the gate bearer is derived from it, and a row created by a writer
            # that sets no id must still hand every concurrent run the same one.
            UpdateExpression="SET hook_token = if_not_exists(hook_token, :h), "
            "gateway_token = if_not_exists(gateway_token, :g), id = if_not_exists(id, :newid)",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":h": hook_token, ":g": gateway_token, ":newid": str(uuid.uuid4())},
            ReturnValues="ALL_NEW",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ProvisionSuperseded(f"no containers row for {owner_id}; its provision claim is gone") from e
        raise
    return response["Attributes"]


async def swap_gate_token_hash(
    owner_id: str,
    *,
    new_hash: str,
    superseded: list[str],
    expected_prior_hash: str | None,
    claim_id: str | None = None,
    attempt_token: str | None = None,
) -> tuple[str, dict | None]:
    """Point the row at a freshly minted gate-token hash, atomically.

    THE ONLY WRITER of ``bedrock_gate_token_hash`` and
    ``bedrock_gate_superseded_hashes``, enforced rather than asserted:
    ``_reject_gate_owned_fields`` refuses either name from ``upsert`` and
    ``update_fields``, and ``upsert``'s whole-item put — which rewrites both
    whether it means to or not — is CAS'd on the pointer to prove it is
    preserving, not overwriting a swap it never saw (``_gate_pointer_unchanged``).
    The pointer used to move inside ``upsert`` too, and no amount of guarding
    made that safe: a mint landing between that read and its unconditional put
    left the snapshot stale whatever the snapshot was read for.

    CONDITIONAL, not merely scoped — which is what it needs over ``update_fields``:
    ``superseded`` is computed from a snapshot the caller read BEFORE minting,
    and two concurrent mints for the same owner (a poll provision plus an
    escalating ``container_recover`` takeover — the mint block sits ahead of
    ``_provision_checkpoint``, so the provision lease does not serialize it)
    would each put back their own snapshot. The loser's hash would vanish from
    the row while its token row lives on with ``denied=False``. The gate-tokens
    table is hash-keyed with no owner index, so that hash is then unreachable
    FOREVER: nothing can expire it and ``deny_gate_token`` cannot cover it —
    a live bearer credential the kill switch cannot revoke. ``list_append``
    would not help; the loser's addition list is computed from the same stale
    snapshot and never mentions the winner's hash either.

    So the write is conditioned on the row still carrying the hash the caller
    read (or on carrying none, when it read none) and the outcome is reported
    back rather than swallowed:

    Returns ``(outcome, item)``, where the conflict state is read off the SAME
    conditional write via ``ReturnValuesOnConditionCheckFailure="ALL_OLD"`` (as
    ``try_claim_owner_deploy`` does) — NOT a follow-up ``get_item``. That is
    load-bearing twice over, because ``get_by_owner_id`` is eventually
    consistent while the condition was evaluated strongly on the leader:

    - Classification. On the ``expected_prior_hash is None`` branch a conflict
      means someone JUST created the row — precisely when a replica read can
      still answer "no row". A follow-up read would call that ``"absent"``, the
      caller would treat it as the benign fresh-provision no-op and never
      retry, and the hash would be orphaned SILENTLY (no error to detect it by).
    - The merge. A stale replica would hand the caller back the pre-swap hash,
      so its retry would fail the identical condition again and burn the budget
      on replication lag rather than on a real race.

    Outcomes:

    - ``("written", None)`` — the swap landed.
    - ``("conflict", item)`` — another writer moved the hash first. ``item`` is
      the authoritative pre-write row: the caller folds its
      ``bedrock_gate_token_hash`` into ``superseded`` and retries against it.
    - ``("absent", None)`` — no row yet. A SILENT no-op, matching
      ``update_fields``: ``mint_gate_token`` can run before the row exists, and
      ``bedrock_gate_tokens.persist_gate_token_hash`` re-points the row through
      this same call once ``create_user_service``'s upsert has created it.

    ``claim_id`` / ``attempt_token`` — optional, and they make this write carry
    the provision lease in its OWN condition rather than behind a checkpoint. A
    checkpoint is "verify, then act", which is inherently non-atomic: a takeover
    landing between the two still let a superseded run move the pointer to its
    own token, leaving a hash no task will ever present as the row's current one
    indefinitely (the drain only ever judges the superseded list). This write is
    a DynamoDB conditional update, so unlike the EFS and task-definition calls
    that follow it, it does not need a checkpoint at all — it can simply refuse.
    A refusal caused by the LEASE clause raises ``ProvisionSuperseded``; one
    caused by the hash clause stays a benign ``"conflict"`` to merge and retry.

    Omitted (the default) means NO lease clause — which is not a weaker mode but
    the correct one for callers that hold no lease: ``mint_gate_token``'s own
    swap, which runs before the row exists on a fresh provision and has no lease
    context, and ``routers/debug.create_user_service(owner_id, gateway_token)``.
    """
    table = _get_table()
    values: dict = {":new": new_hash, ":superseded": superseded, ":iso": utc_now_iso()}
    if expected_prior_hash is None:
        condition = "attribute_exists(owner_id) AND attribute_not_exists(bedrock_gate_token_hash)"
    else:
        condition = "attribute_exists(owner_id) AND bedrock_gate_token_hash = :prior"
        values[":prior"] = expected_prior_hash
    # Strict equality on both, matching what upsert's CAS pins rather than
    # update_container_leased's permissive attempt-token clause: every path that
    # hands a caller an attempt_token (try_claim_provision, takeover_provision,
    # claim_attempt) stamps it on the row in the SAME atomic write, so a row
    # that lacks one while the caller holds one is unreachable — and if it ever
    # happened, refusing fails a provision the poll retries rather than leaving
    # a pointer nothing presents. It also keeps the condition and the failure
    # comparison below mirror images, which is what stops them drifting apart.
    lease_expected: dict = {}
    if claim_id is not None:
        lease_expected["provision_claim_id"] = claim_id
        condition += " AND provision_claim_id = :lease_cid"
        values[":lease_cid"] = claim_id
        if attempt_token is not None:
            lease_expected["provision_attempt_token"] = attempt_token
            condition += " AND provision_attempt_token = :lease_atok"
            values[":lease_atok"] = attempt_token
    try:
        await run_in_thread(
            table.update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET bedrock_gate_token_hash = :new, bedrock_gate_superseded_hashes = :superseded, updated_at = :iso"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
            # The condition is a conjunction, so the failure alone does not say
            # which half missed. DynamoDB answers that here: it returns the row
            # as it stood before the refused write, or nothing at all when
            # there was no row. See the docstring for why this cannot be a
            # follow-up get_item.
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raw = e.response.get("Item")
        if not raw:
            # No row at all. A caller holding a lease has just lost it to a
            # delete; one that holds none is on the benign fresh-provision path.
            if lease_expected:
                _raise_if_superseded(owner_id, lease_expected, {}, where="gate-token swap")
            return "absent", None
        current = _deserialize_item(raw)
        # LEASE FIRST: a moved claim is not a conflict to merge, it is a stop
        # signal — merging would let the superseded run point the row at a token
        # no task of its will ever present. Mirrors upsert's triage exactly.
        if lease_expected:
            _raise_if_superseded(owner_id, lease_expected, current, where="gate-token swap")
        if not current:
            # Unparseable: degrade to "retry against an unknown prior", which
            # fails the condition again and lands in the caller's ERROR log —
            # never a TypeError escaping mint_gate_token, which has no try.
            return "conflict", None
        return "conflict", current
    return "written", None


async def upsert_paired_devices(owner_id: str, entries: dict) -> None:
    """Project desktop device-pubkey entries onto the owner row's ``paired_devices`` map.

    ``entries`` is ``{deviceId: {"publicKey": <b64url-raw>, "userId": <member>,
    "clientId": <str>}}``. It is the ONLY record of a paired device (the container's EFS
    ``paired.json`` went with that runtime) and gives the WebSocket authorizer a
    ``GetItem(containers, owner)`` point lookup for device-key WS-upgrade auth, with no
    separate table.

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
    the authorizer's trust anchor. This IS the guard now; the EFS ``paired.json`` copy of it
    went with that rail.
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


async def touch_last_interactive(owner_id: str, *, at: int | None = None) -> bool:
    """Stamp a durable authenticated-command activity time.

    Reads, typing, AppSync subscriptions, and status polls must not call this.
    Clearing an old idle-stop claim lets a genuine new command win over a stale
    sweep before the ECS stop side effect.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET last_interactive_at = :at, updated_at = :iso REMOVE idle_stop_claim, idle_stop_claimed_at",
            ConditionExpression="attribute_exists(owner_id)",
            ExpressionAttributeValues={":at": int(at if at is not None else time.time()), ":iso": utc_now_iso()},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def try_claim_idle_stop(
    owner_id: str,
    *,
    observed_last_interactive_at: int | None,
    stale_claim_before: int,
) -> str | None:
    """Claim one trial-idle stop against the activity value the sweep read."""
    claim = uuid.uuid4().hex
    condition = "#status = :running AND (attribute_not_exists(idle_stop_claimed_at) OR idle_stop_claimed_at < :stale)"
    values: dict = {
        ":running": "running",
        ":stale": int(stale_claim_before),
        ":claim": claim,
        ":now": int(time.time()),
        ":iso": utc_now_iso(),
    }
    if observed_last_interactive_at is None:
        condition += " AND attribute_not_exists(last_interactive_at)"
    else:
        condition += " AND last_interactive_at = :observed"
        values[":observed"] = int(observed_last_interactive_at)
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET idle_stop_claim = :claim, idle_stop_claimed_at = :now, updated_at = :iso",
            ConditionExpression=condition,
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues=values,
        )
        return claim
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


async def idle_stop_claim_current(owner_id: str, claim: str) -> bool:
    row = await get_by_owner_id(owner_id, consistent=True)
    return bool(row and row.get("status") == "running" and row.get("idle_stop_claim") == claim)


async def update_error(owner_id: str, error: str) -> dict | None:
    """Record the last error for a container."""
    return await update_fields(
        owner_id,
        {
            "last_error": error,
            "last_error_at": utc_now_iso(),
        },
    )


async def delete(owner_id: str) -> dict | None:
    """Delete the owner's containers row; return the item as it was, or None.

    ``ALL_OLD`` because this row is the ONLY handle on some of the owner's
    other state — most sharply the bedrock-gate token hashes, whose table is
    keyed on the hash with no owner index. A caller tearing an account down
    (``core.services.account_wipe``) reads those off the returned image and
    sweeps them: anything written onto the row after that caller last read it
    is otherwise unreachable the instant this delete lands.
    """
    table = _get_table()
    resp = await run_in_thread(table.delete_item, Key={"owner_id": owner_id}, ReturnValues="ALL_OLD")
    # ``or None``: DynamoDB omits Attributes when there was no row, but moto
    # returns an empty map — one answer for "there was nothing here" either way.
    return resp.get("Attributes") or None


BROWSER_KEY_PREFIX = "__browser__#"


async def get_browser_state(owner_id: str) -> dict | None:
    row = await get_by_owner_id(f"{BROWSER_KEY_PREFIX}{owner_id}", consistent=True)
    return (row or {}).get("browser_state")


async def delete_browser_state(owner_id: str) -> None:
    await run_in_thread(_get_table().delete_item, Key={"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"})


async def compare_browser_state(owner_id: str, expected: dict | None, state: dict) -> bool:
    """Fence browser metadata independently of disposable runtime rows, in the same table.

    The parent condition stops a late checkpoint resurrecting data after account wipe.
    Reserved keys are already excluded by scan_all; no new table or GSI is needed.
    """
    values = {":next": state}
    if expected is None:
        condition = "attribute_not_exists(browser_state)"
    else:
        condition = "browser_state = :expected"
        values[":expected"] = expected
    table = _get_table()
    try:
        await run_in_thread(
            table.meta.client.transact_write_items,
            TransactItems=[
                {
                    "ConditionCheck": {
                        "TableName": table.name,
                        "Key": {"owner_id": owner_id},
                        "ConditionExpression": "attribute_exists(owner_id) AND attribute_not_exists(release_update_id)",
                    }
                },
                {
                    "Update": {
                        "TableName": table.name,
                        "Key": {"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"},
                        "UpdateExpression": "SET browser_state = :next",
                        "ConditionExpression": condition,
                        "ExpressionAttributeValues": values,
                    }
                },
            ],
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "TransactionCanceledException":
            reasons = exc.response.get("CancellationReasons", [])
            if any(r.get("Code") == "ConditionalCheckFailed" for r in reasons):
                return False
        raise
