"""Container repository -- DynamoDB operations for the containers table."""

import asyncio
import logging
import random
import time
import uuid
from decimal import Decimal

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


def _get_table():
    return get_table("containers")


async def get_by_owner_id(owner_id: str, *, consistent: bool = False) -> dict | None:
    """Read the owner's container row. Eventually consistent BY DEFAULT.

    ``consistent=True`` is opt-in per call, never the default: this is the
    hottest read in the system (``GET /container/status`` polls it every ~3s
    per signed-in user), and a strongly consistent GetItem costs double the
    RCUs. Ask for it only where a stale answer is a correctness bug rather than
    a slightly old status.
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

    Used by fleet-wide ops tools (the operator console's fleet grid and the
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


# The two fields ``swap_gate_token_hash`` owns outright. Enforced, not merely
# documented: the CAS below conditions on the POINTER, so a caller naming only
# the superseded list would slip past it and silently replace the list with the
# pointer unchanged — and every hash dropped from it is a live bearer credential
# nothing can find, expire, or deny again.
_GATE_OWNED_FIELDS = frozenset({"bedrock_gate_token_hash", "bedrock_gate_superseded_hashes", "agent_gate_token_hashes"})


async def add_agent_gate_hash(owner_id: str, token_hash: str) -> None:
    """Track a per-agent gate-token hash on the owner's row (string set ADD: idempotent).

    A separate field from the superseded list on purpose: that list is drained by the boot-secret
    liveness predicate, which knows only the owner-level bearer and would delete agent rows.
    ``owner_gate_token_hashes`` includes this set, so deny and wipe reach every agent bearer."""
    await run_in_thread(
        _get_table().update_item,
        Key={"owner_id": owner_id},
        UpdateExpression="ADD agent_gate_token_hashes :h",
        ConditionExpression="attribute_exists(owner_id)",
        ExpressionAttributeValues={":h": {token_hash}},
    )


def _reject_gate_owned_fields(caller: str, fields: dict) -> None:
    named = sorted(_GATE_OWNED_FIELDS & fields.keys())
    if named:
        raise ValueError(
            f"{caller}: {named} may only be written by swap_gate_token_hash — a write from "
            "anywhere else can erase a hash the bedrock-gate kill switch still needs to reach"
        )


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


async def try_consume_stranger_wake(
    owner_id: str, *, sender_key: str, day: str, owner_cap: int, sender_cap: int, attr_prefix: str = "stranger_wake"
) -> bool:
    """Admit one inbound email from a NON-allowlisted sender to WAKE the owner's agent for ``day``.

    Open email lets any authenticated sender start an agent turn, and a turn costs real money
    (a trial's daily allowance, a paid owner's credits), so strangers are bounded twice: at most
    ``sender_cap`` wakes per sender AND ``owner_cap`` across all strangers, per UTC day. Both caps
    are checked and both counters bumped in ONE conditional UpdateItem, so a wake is admitted under
    both limits or under neither. ``sender_key`` is a hash, never the address (it becomes a map key).

    DURABLE for the same reason as ``try_consume_daily_email_send``: several backend tasks run
    behind one ALB. State lives on the owner's containers row: ``stranger_wake_day``, a total
    ``stranger_wake_count`` and a ``stranger_wake_senders`` map (at most ``owner_cap`` keys a day).

    Returns True when the wake is admitted. Unlike the send counter this **fails CLOSED**: a fault
    or a missing container row returns False. The mail is already stored, so a false "no" costs one
    wake, whereas a false "yes" spends inference money on an unbounded stranger.
    """
    table = _get_table()
    key = {"owner_id": owner_id}
    day_a, count_a, senders_a = f"{attr_prefix}_day", f"{attr_prefix}_count", f"{attr_prefix}_senders"

    async def _bump() -> bool:
        try:
            await run_in_thread(
                table.update_item,
                Key=key,
                UpdateExpression=(
                    f"SET {count_a} = if_not_exists({count_a}, :zero) + :one, "
                    f"{senders_a}.#k = if_not_exists({senders_a}.#k, :zero) + :one"
                ),
                ConditionExpression=(
                    f"{day_a} = :d"
                    f" AND (attribute_not_exists({count_a}) OR {count_a} < :ocap)"
                    f" AND (attribute_not_exists({senders_a}.#k) OR {senders_a}.#k < :scap)"
                ),
                ExpressionAttributeNames={"#k": sender_key},
                ExpressionAttributeValues={":d": day, ":zero": 0, ":one": 1, ":ocap": owner_cap, ":scap": sender_cap},
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    try:
        if await _bump():
            return True
        # Refused: either a limit is reached, or the stored day is older than today / absent (the
        # first stranger of the day). Roll the window FORWARD (`<`, never back: a task holding a
        # stale day must not reset a window another task already advanced), then bump again. If
        # the rollover is refused, either another task just rolled it (the bump now succeeds) or
        # the day was current and a limit was really reached (the bump is refused again).
        try:
            await run_in_thread(
                table.update_item,
                Key=key,
                UpdateExpression=f"SET {day_a} = :d, {count_a} = :zero, {senders_a} = :empty",
                ConditionExpression=(f"attribute_exists(owner_id) AND (attribute_not_exists({day_a}) OR {day_a} < :d)"),
                ExpressionAttributeValues={":d": day, ":zero": 0, ":empty": {}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        return await _bump()
    except Exception:  # noqa: BLE001 — fail closed, see docstring
        logger.warning("try_consume_stranger_wake failed for %s", owner_id, exc_info=True)
        return False


async def release_stranger_wake(
    owner_id: str, *, sender_key: str, day: str, attr_prefix: str = "stranger_wake"
) -> None:
    """Give back the slot ``try_consume_stranger_wake`` admitted when what it admitted could not be sent,
    so the delivery's retry is admitted again. Best effort, and only inside the same day's window: a slot
    the window has rolled past, or a sender never admitted, is left alone."""
    day_a, count_a, senders_a = f"{attr_prefix}_day", f"{attr_prefix}_count", f"{attr_prefix}_senders"
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=f"SET {count_a} = {count_a} - :one, {senders_a}.#k = {senders_a}.#k - :one",
            ConditionExpression=f"{day_a} = :d AND {senders_a}.#k >= :one",
            ExpressionAttributeNames={"#k": sender_key},
            ExpressionAttributeValues={":d": day, ":one": 1},
        )
    except Exception as e:  # noqa: BLE001 — a slot not given back costs one retry's reply, never the ingestion
        if getattr(e, "response", {}).get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            logger.warning("release_stranger_wake failed for %s", owner_id, exc_info=True)


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


async def release_pass(name: str) -> None:
    """Re-open pass ``name`` now: a holder whose work failed lets the retry in before the window ends."""
    await run_in_thread(_get_table().delete_item, Key={"owner_id": f"{PASS_KEY_PREFIX}{name}"})


HUDDLE_KEY_PREFIX = "__huddle__#"


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


async def try_claim_owner_lifecycle(owner_id: str, *, stale_s: int = LIFECYCLE_CLAIM_STALE_S) -> str | None:
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
                ":cutoff": now - stale_s,
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


async def mark_oauth_revoked(owner_id: str) -> int:
    """Revoke-all's mark: an OAuth connect whose state was minted at or before it is refused
    (``routers.connections.oauth_callback``). On the lease row, which the release leaves alone
    (it removes only ``claimed_at``/``tok``). Milliseconds, so a connect started later in the
    same second is not refused."""
    now = int(time.time() * 1000)
    await run_in_thread(
        _get_table().update_item,
        Key={"owner_id": f"{LIFECYCLE_KEY_PREFIX}{owner_id}"},
        UpdateExpression="SET oauth_revoked_at_ms = :now",
        ExpressionAttributeValues={":now": now},
    )
    return now


async def get_oauth_revoked_at(owner_id: str) -> int | None:
    row = await get_by_owner_id(f"{LIFECYCLE_KEY_PREFIX}{owner_id}", consistent=True)
    value = (row or {}).get("oauth_revoked_at_ms")
    return int(value) if value is not None else None


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
    incident).

    ``marketplace_hire.create_hire_approval`` also takes this claim before its
    Paperclip pre-create and approval reconciliation. It installs no workspace,
    but must serialize with those paths so concurrent recommendations cannot
    file duplicate approvals for one logical hire.

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
      overlap window this exists to close.
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
    than a new table, matching ``try_consume_daily_email_send`` on the same row.

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


def _has_timeout_cause(exc: BaseException | None) -> bool:
    """True when ``exc`` is a ``TimeoutError`` or was raised ``from`` one.

    Deploy callers wrap the failure before it reaches the claim release — the
    marketplace router re-raises as ``HTTPException(500) from exc`` — so the timeout that matters usually sits one link
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


async def mark_provisioning(owner_id: str) -> None:
    """Show a just-dispatched provision on the row, creating the row if absent.

    A display projection, not a lease: nothing reads it to decide who may
    provision (a RUNNING Step Functions execution is that answer). A ``running``
    row is left alone -- a reconcile dispatch against a live workspace must not
    flip its UI into the provisioning stepper.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET #s = :prov, substatus = :sub, updated_at = :iso, "
                "id = if_not_exists(id, :newid), created_at = if_not_exists(created_at, :iso)"
            ),
            ConditionExpression="attribute_not_exists(owner_id) OR #s <> :running",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":prov": "provisioning",
                ":sub": "queued",
                ":iso": utc_now_iso(),
                ":newid": str(uuid.uuid4()),
                ":running": "running",
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    publish_status_changed(owner_id)


async def mark_provision_error(owner_id: str, *, last_error: str, retryable: bool) -> None:
    """Record a failed provision: ``status="error"`` with its cause.

    ``retryable`` records the cause's class (True transient, False deterministic);
    either way the row waits for the user's Retry (``/container/recover``).
    Never overwrites a ``running`` row -- a concurrent run that already
    succeeded wins, and a failed reconcile of a live workspace leaves it live.
    No-op when the row is gone.
    """
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #s = :err, substatus = :sub, #rt = :rt, #le = :le, updated_at = :iso",
            ConditionExpression="attribute_exists(owner_id) AND #s <> :running",
            ExpressionAttributeNames={"#s": "status", "#rt": "retryable", "#le": "last_error"},
            ExpressionAttributeValues={
                ":err": "error",
                ":sub": "provision_failed",
                ":rt": retryable,
                ":le": str(last_error)[:500],
                ":running": "running",
                ":iso": utc_now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise
    publish_status_changed(owner_id)


async def note_provision_error(owner_id: str, last_error: str) -> None:
    """Record WHY a provision attempt failed without changing the row's status (the machine
    retries a transient failure; ``MarkFailedExhausted`` flips it to error later and does not
    touch ``last_error``, so this is what the status banner shows). No-op on a running/gone row."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression="SET #le = :le, #lea = :iso, updated_at = :iso",
            ConditionExpression="attribute_exists(owner_id) AND #s <> :running",
            ExpressionAttributeNames={"#s": "status", "#le": "last_error", "#lea": "last_error_at"},
            ExpressionAttributeValues={
                ":le": " ".join(str(last_error).split())[:500],
                ":running": "running",
                ":iso": utc_now_iso(),
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


def classify_provision_error(exc: BaseException) -> tuple[str, bool]:
    """Map a caught provision failure to ``(last_error, retryable)`` for
    ``mark_provision_error``.

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


async def update_status(owner_id: str, status: str, substatus: str | None = None) -> dict | None:
    fields = {"status": status, "substatus": substatus}
    return await update_fields(owner_id, fields)


async def update_fields(
    owner_id: str, fields: dict, *, expected_field: str | None = None, expected_value=None
) -> dict | None:
    """Merge ``fields`` into an EXISTING row. Returns the updated item, or
    ``None`` when there is no row or ``expected_field`` no longer holds ``expected_value``.

    A SCOPED ``update_item`` — it writes the caller's fields and nothing else.
    It used to be a get→put of the whole item, which resurrected a concurrent
    writer's ``status`` or gate-token pointer from a read milliseconds old.

    ``expected_field``/``expected_value`` — the row must still hold this value
    (plain equality) for the write to land. For a write that CLEARS a value a
    concurrent run may already have replaced, e.g. the CREATE_FAILED ARN clear
    in ``hermes_provisioning``.

    EVERY attribute name is aliased, uniformly: ``status`` is a DynamoDB
    reserved word and the commonest field written here. A ``None`` value is
    written as NULL, never REMOVE'd. A write that sets ``status="running"``
    also REMOVEs ``last_error``/``last_error_at``/``retryable``.
    """
    _reject_gate_owned_fields("update_fields", fields)
    names: dict[str, str] = {}
    set_parts: list[str] = []
    values: dict = {":iso": utc_now_iso()}
    for i, (key, value) in enumerate(fields.items()):
        names[f"#f{i}"] = key
        set_parts.append(f"#f{i} = :v{i}")
        values[f":v{i}"] = value
    set_parts.append("updated_at = :iso")
    condition = "attribute_exists(owner_id)"
    if expected_field is not None:
        names["#e"], values[":e"] = expected_field, expected_value
        condition += " AND #e = :e"
    update_expression = "SET " + ", ".join(set_parts)
    if fields.get("status") == "running":
        # A row that reached running carries no error from an earlier failed attempt.
        update_expression += " REMOVE last_error, last_error_at, retryable"
    kwargs: dict = {
        "Key": {"owner_id": owner_id},
        "UpdateExpression": update_expression,
        "ConditionExpression": condition,
        "ExpressionAttributeValues": values,
        "ReturnValues": "ALL_NEW",
    }
    if names:  # DDB rejects an empty map; an empty `fields` still stamps updated_at
        kwargs["ExpressionAttributeNames"] = names
    try:
        response = await run_in_thread(_get_table().update_item, **kwargs)
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    publish_status_changed(owner_id)
    return response.get("Attributes")


async def ensure_owner_tokens(owner_id: str, *, hook_token: str, gateway_token: str) -> dict:
    """Set ``hook_token``/``gateway_token`` (and ``id``) only where the row has none; return the row.

    ONE ``if_not_exists`` UpdateItem, so concurrent provision runs each offering fresh
    tokens converge on the first writer's pair, and an owner that already has tokens keeps
    them (they are snapshotted into Paperclip adapters, so rotating one silently breaks
    every run). Callers use the RETURNED values, never their own candidates.

    Raises when there is no row (dispatch creates it; a teardown deleted it mid-provision):
    an UpdateItem would otherwise create a token-only ghost row.
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
            raise RuntimeError(f"no containers row for {owner_id}; it was deleted mid-provision") from e
        raise
    return response["Attributes"]


async def swap_gate_token_hash(
    owner_id: str,
    *,
    new_hash: str,
    superseded: list[str],
    expected_prior_hash: str | None,
) -> tuple[str, dict | None]:
    """Point the row at a freshly minted gate-token hash, atomically.

    THE ONLY WRITER of ``bedrock_gate_token_hash`` and
    ``bedrock_gate_superseded_hashes``, enforced: ``_reject_gate_owned_fields``
    refuses either name from ``update_fields``.

    CONDITIONAL, not merely scoped: ``superseded`` is computed from a snapshot
    the caller read BEFORE minting, and two concurrent mints would each put back
    their own snapshot. The loser's hash would vanish from the row while its
    token row lives on with ``denied=False`` -- and the gate-tokens table is
    hash-keyed with no owner index, so that hash is unreachable FOREVER: a live
    bearer credential the kill switch cannot revoke.

    So the write is conditioned on the row still carrying the hash the caller
    read (or none, when it read none), and the conflict state is read off the
    SAME write via ``ReturnValuesOnConditionCheckFailure="ALL_OLD"`` -- NOT a
    follow-up eventually-consistent ``get_item``, which could call a
    just-created row "absent" (orphaning the hash silently) or hand back the
    pre-swap hash (burning the retry budget on replication lag).

    Outcomes:

    - ``("written", None)`` — the swap landed.
    - ``("conflict", item)`` — another writer moved the hash first. ``item`` is
      the authoritative pre-write row: the caller folds its
      ``bedrock_gate_token_hash`` into ``superseded`` and retries against it.
    - ``("absent", None)`` — no row yet. A SILENT no-op, matching
      ``update_fields``.
    """
    values: dict = {":new": new_hash, ":superseded": superseded, ":iso": utc_now_iso()}
    if expected_prior_hash is None:
        condition = "attribute_exists(owner_id) AND attribute_not_exists(bedrock_gate_token_hash)"
    else:
        condition = "attribute_exists(owner_id) AND bedrock_gate_token_hash = :prior"
        values[":prior"] = expected_prior_hash
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": owner_id},
            UpdateExpression=(
                "SET bedrock_gate_token_hash = :new, bedrock_gate_superseded_hashes = :superseded, updated_at = :iso"
            ),
            ConditionExpression=condition,
            ExpressionAttributeValues=values,
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raw = e.response.get("Item")
        if not raw:
            return "absent", None
        current = _deserialize_item(raw)
        if not current:
            # Unparseable: degrade to "retry against an unknown prior", which
            # fails the condition again and lands in the caller's ERROR log —
            # never a TypeError escaping mint_gate_token, which has no try.
            return "conflict", None
        return "conflict", current
    return "written", None


async def retry_conflict(call):
    """Await ``call()`` again while DynamoDB refuses it only because another
    transaction holds the item: a plain write raises ``TransactionConflictException``,
    a transaction is cancelled with ``TransactionConflict`` and no failed condition.
    A failed condition is a real answer and raises at once. Full-jitter backoff,
    25 ms doubling, 500 ms cap per sleep, 8 tries (~1.8 s worst case)."""
    # ponytail: every pairing of a workspace writes ONE owner row, so its pairings and
    # enrollments run in one lane; move paired_devices to per-device rows if a workspace
    # ever pairs dozens of Macs at the same instant.
    for attempt in range(8):
        try:
            return await call()
        except ClientError as exc:
            error = exc.response.get("Error", {}).get("Code")
            codes = {r.get("Code") for r in exc.response.get("CancellationReasons", [])}
            conflict = error == "TransactionConflictException" or (
                error == "TransactionCanceledException"
                and "TransactionConflict" in codes
                and "ConditionalCheckFailed" not in codes
            )
            if not conflict or attempt == 7:
                raise
            await asyncio.sleep(random.uniform(0, min(0.5, 0.025 * 2**attempt)))


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
        await retry_conflict(
            lambda: run_in_thread(
                table.update_item,
                Key={"owner_id": owner_id},
                UpdateExpression="SET #pd = if_not_exists(#pd, :empty)",
                ConditionExpression="attribute_exists(owner_id)",
                ExpressionAttributeNames={"#pd": "paired_devices"},
                ExpressionAttributeValues={":empty": {}},
            )
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
        await retry_conflict(
            lambda: run_in_thread(
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
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Row deleted mid-write, or deviceId owned by another member. The caller
            # (desktop_pair.pair_desktop_devices) re-reads the row and names the latter.
            return
        raise


async def remove_paired_device(owner_id: str, device_id: str) -> None:
    """Remove a device from the owner row's ``paired_devices`` map (revocation).

    Scoped ``REMOVE`` of the single map key so a racing writer of other row
    attributes cannot resurrect the revoked entry. No-op if the row is absent.
    """
    table = _get_table()
    try:
        await retry_conflict(
            lambda: run_in_thread(
                table.update_item,
                Key={"owner_id": owner_id},
                UpdateExpression="REMOVE #pd.#did",
                ConditionExpression="attribute_exists(owner_id)",
                ExpressionAttributeNames={"#pd": "paired_devices", "#did": device_id},
            )
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no container row — nothing to remove
        raise


async def mark_paired_device_revoking(owner_id: str, device_id: str, *, user_id: str | None = None) -> bool:
    """Flag a paired device as being revoked: every command resolution refuses it from now
    on (``connection_service``), except the one internal clear that runs before the entry
    is removed. With ``user_id``, only while the entry still belongs to that member (a
    re-pair rewrites the whole entry). False when not paired (or re-paired meanwhile)."""
    condition = "attribute_exists(#pd.#did)"
    names = {"#pd": "paired_devices", "#did": device_id, "#rv": "revoking"}
    values: dict = {":t": True}
    if user_id is not None:
        condition += " AND #pd.#did.#uid = :uid"
        names["#uid"] = "userId"
        values[":uid"] = user_id
    try:
        await retry_conflict(
            lambda: run_in_thread(
                _get_table().update_item,
                Key={"owner_id": owner_id},
                UpdateExpression="SET #pd.#did.#rv = :t",
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


async def remove_revoked_paired_device(owner_id: str, device_id: str, user_id: str) -> bool:
    """Remove a pairing only while it is still that member's REVOKING entry: a fresh re-pair
    (which rewrites the entry without the flag) survives. False when it did not match."""
    try:
        await retry_conflict(
            lambda: run_in_thread(
                _get_table().update_item,
                Key={"owner_id": owner_id},
                UpdateExpression="REMOVE #pd.#did",
                ConditionExpression="#pd.#did.#rv = :t AND #pd.#did.#uid = :uid",
                ExpressionAttributeNames={
                    "#pd": "paired_devices",
                    "#did": device_id,
                    "#rv": "revoking",
                    "#uid": "userId",
                },
                ExpressionAttributeValues={":t": True, ":uid": user_id},
            )
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False


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


async def touch_browser_input(owner_id: str, at: float, field: str = "human_input_at", *, create: bool = False) -> None:
    """Record a person's latest input in the live view, beside (never inside) browser_state:
    the panel posts it every couple of seconds, and folding it into the fenced map would
    fail every in-flight agent claim's compare-and-set. ``field="human_view_at"`` records the
    open panel's viewer heartbeat the same way. ``create``: also for a workspace that never
    started a cloud browser (live frames of a Mac tab)."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"},
            UpdateExpression=f"SET {field} = :at",
            **({} if create else {"ConditionExpression": "attribute_exists(browser_state)"}),
            ExpressionAttributeValues={":at": Decimal(str(round(at, 3)))},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_browser_input_at(owner_id: str, field: str = "human_input_at") -> float | None:
    row = await get_by_owner_id(f"{BROWSER_KEY_PREFIX}{owner_id}", consistent=True)
    value = (row or {}).get(field)
    return float(value) if value is not None else None


async def set_browser_viewport(owner_id: str, width: int, height: int) -> None:
    """The size the panel last asked for, beside browser_state for the same reason as
    ``human_input_at``: the next session starts at it, and an agent run never races it."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"},
            UpdateExpression="SET viewport = :v",
            ConditionExpression="attribute_exists(browser_state)",
            ExpressionAttributeValues={":v": {"width": width, "height": height}},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_browser_viewport(owner_id: str) -> dict | None:
    row = await get_by_owner_id(f"{BROWSER_KEY_PREFIX}{owner_id}", consistent=True)
    value = (row or {}).get("viewport")
    return {"width": int(value["width"]), "height": int(value["height"])} if value else None


async def delete_browser_state(owner_id: str) -> None:
    await run_in_thread(_get_table().delete_item, Key={"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"})


async def set_browser_helper(owner_id: str, stamp: dict) -> None:
    """When the helper/policy objects were last written (``browser_extensions.session_config``).
    Beside browser_state like the viewport; a lost race just rewrites the same objects."""
    try:
        await run_in_thread(
            _get_table().update_item,
            Key={"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"},
            UpdateExpression="SET browser_helper = :v",
            ConditionExpression="attribute_exists(browser_state)",
            ExpressionAttributeValues={":v": stamp},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise


async def get_browser_extensions(owner_id: str) -> list[dict] | None:
    """The stored list as-is (None when never set): pass it back as ``expected``."""
    row = await get_by_owner_id(f"{BROWSER_KEY_PREFIX}{owner_id}", consistent=True)
    return (row or {}).get("browser_extensions")


async def compare_browser_extensions(owner_id: str, expected: list[dict] | None, extensions: list[dict]) -> bool:
    """The workspace's browser extension list, beside (never inside) browser_state so an admin
    edit never fails an in-flight session claim's compare-and-set. Same fences as below."""
    return await _compare_browser_attr(owner_id, "browser_extensions", expected, extensions)


async def compare_browser_state(owner_id: str, expected: dict | None, state: dict) -> bool:
    """Fence browser metadata independently of disposable runtime rows, in the same table.

    The parent condition stops a late checkpoint resurrecting data after account wipe.
    Reserved keys are already excluded by scan_all; no new table or GSI is needed.
    """
    return await _compare_browser_attr(owner_id, "browser_state", expected, state)


_CONFLICT_RETRIES = 3


async def _compare_browser_attr(owner_id: str, attr: str, expected, value) -> bool:
    values = {":next": value}
    if expected is None:
        condition = f"attribute_not_exists({attr})"
    else:
        condition = f"{attr} = :expected"
        values[":expected"] = expected
    table = _get_table()
    items = [
        {
            "ConditionCheck": {
                "TableName": table.name,
                "Key": {"owner_id": owner_id},
                "ConditionExpression": "attribute_exists(owner_id)",
            }
        },
        {
            "Update": {
                "TableName": table.name,
                "Key": {"owner_id": f"{BROWSER_KEY_PREFIX}{owner_id}"},
                "UpdateExpression": f"SET {attr} = :next",
                "ConditionExpression": condition,
                "ExpressionAttributeValues": values,
            }
        },
    ]
    for attempt in range(_CONFLICT_RETRIES + 1):
        try:
            await run_in_thread(table.meta.client.transact_write_items, TransactItems=items)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "TransactionCanceledException":
                raise
            codes = {r.get("Code") for r in exc.response.get("CancellationReasons", [])}
            if "ConditionalCheckFailed" in codes:
                return False
            # Another transaction was writing the same row at that instant (two runs, or a run
            # and the autosave sweep; prod 2026-09-27 answered these as 503s). Once it lands our
            # condition is decidable: retry, and if the row stays contended, "it changed".
            if "TransactionConflict" not in codes:
                raise
            if attempt < _CONFLICT_RETRIES:
                await asyncio.sleep(0.05 * 2**attempt)
    return False


# The owner's AgentCore session estimate + cold-wake lease (``runtime_transport``'s single-flight
# wake). Its own reserved row, like the browser row above: the owner row a caller holds may
# predate a stop, and a stale "warm" read is the one direction that re-opens the split-brain race.
SESSION_KEY_PREFIX = "__session__#"
SESSION_TTL_MARGIN_S = 3600


def _session_key(owner_id: str, task_id: str | None = None) -> dict:
    """``__session__#<owner>``, or ``__session__#<owner>#task#<task>`` for a per-task session:
    one row per AgentCore session, so a task's wake never blocks (or warms) the workspace's."""
    suffix = f"#task#{task_id}" if task_id is not None else ""
    return {"owner_id": f"{SESSION_KEY_PREFIX}{owner_id}{suffix}"}


def _conditional_miss(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


def get_session_sync(owner_id: str, *, task_id: str | None = None) -> dict:
    """Strongly consistent; ``{}`` when there is no row. Sync -- the wake runs on the invoke pool."""
    return _get_table().get_item(Key=_session_key(owner_id, task_id), ConsistentRead=True).get("Item") or {}


def try_take_wake_lease_sync(
    owner_id: str, *, arn: str, token: str, now: int, lease_s: int, task_id: str | None = None
) -> bool:
    """Take the cold-wake lease: only when nobody holds a live one AND the session is not warm
    on this runtime."""
    update = "SET waking_until = :lease, wake_tok = :tok"
    values: dict = {":lease": now + lease_s, ":tok": token, ":now": now, ":arn": arn}
    if task_id is not None:
        # One row per task would otherwise outlive its task forever: a session is gone 8 h after
        # its claim (AgentCore maxLifetime), and every new claim takes this lease again.
        from core.services.agentcore_runtime import MAX_LIFETIME_S

        update += ", session_ttl = :ttl"
        values[":ttl"] = now + MAX_LIFETIME_S + SESSION_TTL_MARGIN_S
    try:
        _get_table().update_item(
            Key=_session_key(owner_id, task_id),
            UpdateExpression=update,
            ConditionExpression=(
                "(attribute_not_exists(waking_until) OR waking_until < :now)"
                " AND (attribute_not_exists(warm_until) OR warm_until <= :now OR arn <> :arn)"
            ),
            ExpressionAttributeValues=values,
        )
        return True
    except ClientError as exc:
        if _conditional_miss(exc):
            return False
        raise


def finish_wake_sync(
    owner_id: str,
    *,
    token: str,
    warm_until: int | None = None,
    started_at: int | None = None,
    arn: str | None = None,
    task_id: str | None = None,
) -> None:
    """Drop the lease we hold; with ``warm_until`` also record the session as warm on ``arn``.
    A lease taken over (expired) or cleared by a stop is left alone."""
    update, values = "REMOVE waking_until, wake_tok", {":tok": token}
    if warm_until is not None:
        update = "SET warm_until = :w, started_at = :s, arn = :arn " + update
        values.update({":w": warm_until, ":s": started_at, ":arn": arn})
    try:
        _get_table().update_item(
            Key=_session_key(owner_id, task_id),
            UpdateExpression=update,
            ConditionExpression="wake_tok = :tok",
            ExpressionAttributeValues=values,
        )
    except ClientError as exc:
        if not _conditional_miss(exc):
            raise


def extend_session_warm_sync(owner_id: str, *, warm_until: int, task_id: str | None = None) -> None:
    """Move ``warm_until`` forward; never resurrect a row a stop cleared."""
    try:
        _get_table().update_item(
            Key=_session_key(owner_id, task_id),
            UpdateExpression="SET warm_until = :w",
            ConditionExpression="attribute_exists(warm_until) AND warm_until < :w",
            ExpressionAttributeValues={":w": warm_until},
        )
    except ClientError as exc:
        if not _conditional_miss(exc):
            raise


async def clear_session(owner_id: str, *, task_id: str | None = None) -> None:
    """Forget the session: the next invoke takes the cold path (one serialized ``health``)."""
    await run_in_thread(_get_table().delete_item, Key=_session_key(owner_id, task_id))
