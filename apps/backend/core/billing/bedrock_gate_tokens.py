"""Per-owner bedrock-gate bearer tokens — mint / expire / deny.

The bedrock-gate Lambda authenticates every inbound Bedrock request by
SHA-256-hashing the caller's ``Authorization: Bearer <token>`` and GetItem-ing a
row in the per-env gate-tokens table keyed on that hash. This module is the
backend WRITER of those rows.

Hash-only storage: the plaintext token exists exactly ONCE — at mint time, when
it is returned to the caller for injection into the container's ECS
task-definition env (``AWS_BEARER_TOKEN_BEDROCK``). Nothing persists the
plaintext (mirrors ``api_key_repo`` / hook-token posture): a leaked table row
cannot be turned back into a usable token, and a compromised token is handled by
rotate (reprovision) or deny (kill switch), never by reading it back.

The token row's ``aip_tiers`` map is the INVERSE of the owner's
``bedrock_aip_arns`` map. The gateway authorizes + prices a request by looking
up the request's model-id — which is an AIP ARN — in ``aip_tiers`` to recover
its tier, so the map is keyed ``{aip_arn: tier}`` whereas the owner's containers
row stores ``{tier: aip_arn}``.

Table naming: the gate-tokens table is GATE-PRIVATE state, defined in the
bedrock-gate CDK stack (NOT database-stack), so — unlike ``bedrock-aip-owners``
/ ``metronome-fast-gate``, whose full names arrive as CDK-injected env vars —
the backend constructs it by convention from the env name, matching the gateway
Lambda's own ``TOKENS_TABLE`` and ``bedrock_aip._aip_name``'s ``isol8-{env}-``
scheme. An explicit ``BEDROCK_GATE_TOKENS_TABLE`` override wins (tests / manual
wiring).

Lifetime: a token row lives until NOTHING CAN PRESENT IT. The token reaches a
container only as an inline env var in its ECS task definition, frozen at task
start, so the DDB side and the container side are coupled only by a redeploy
that actually lands. Minting therefore schedules nothing; a superseded row is
deleted only once ``_presentable_gate_token_hashes`` confirms that neither the
owner's ECS **service task definition** (what will launch next — this covers
scale-to-zero, crash/restart, and a roll that never landed) nor any **task ECS
has not finished stopping** still carries it. The service-task-definition half
is what actually decides things here; the live-task half is a correctness guard
that the evaluation points make inert in practice — see that function's
docstring before assuming it fires.

``deny_gate_token`` (the kill switch) covers the SAME set, current hash plus
tracked superseded hashes. It has to: a superseded token now outlives the mint
indefinitely *because* something can still present it, so denying only the
current hash would leave the presentable one live.

This replaced a flat one-hour TTL set at mint time, which killed a prod
container for good: the reprovision minted a new token, put the old one on a
60-minute fuse, and then the service never rolled onto the new task definition —
so the live container kept presenting a credential that DynamoDB reaped out from
under it an hour later. Every evaluation here therefore fails toward
AVAILABILITY: any error leaves the row alone. A stale row is a token nobody
holds; a deleted live row is a customer with zero inference.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from typing import Any

import boto3
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import run_in_thread, utc_now_iso
from core.repositories import container_repo

logger = logging.getLogger(__name__)

# Containers-row attribute holding the hashes this owner has superseded but not
# yet proven dead. The gate-tokens table is keyed on the hash and has no
# owner-side index, so nothing else can find an owner's old rows — this list is
# the ONLY handle on them, for deletion AND for ``deny_gate_token``.
#
# Self-bounding, which is why it needs no cap or sweep: it is only ever written
# through ``_swap_hash_pointer``, which writes the pruned survivor set — and a
# hash only survives a prune while something can still present it, which is at
# most the service's task definition plus whatever tasks are live. (A swap that
# loses the race adds the winner's hash on top of that, but the winner's own
# list was pruned the same way, so the bound holds.)
#
# That holds only while the list SURVIVES every write to the containers row.
# ``container_repo.upsert`` is a whole-item put, so the field is named in its
# ``_UPSERT_PRESERVED_FIELDS`` — without that, a reprovision through
# ``create_user_service`` would silently drop the list and strand its rows
# forever (they are live bearer credentials: the plaintext is still in the
# retained task-definition revision).
_SUPERSEDED_FIELD = "bedrock_gate_superseded_hashes"

# The env var whose value the predicate hashes. ``ecs_manager`` injects the same
# token twice — here and as ``ANTHROPIC_AUTH_TOKEN`` for the Claude Code ACP
# harness — but the harness pair is PAID-ONLY (skipped for bedrock_trial and for
# revoked owners), so reading it would make a free-tier task definition look
# tokenless and expire a token its container is actively using. This one is
# injected for every Bedrock owner whenever a gate URL resolves.
_GATE_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"

# Hash chars logged for correlation against a `describe-task-definition` byte
# comparison -- long enough to be useful for that, short enough that a log
# reader can never reconstruct or brute-force the plaintext from it.
_HASH_PREFIX_LEN = 12

# Appended to every "the ECS answer was incomplete" RuntimeError. The predicate
# has exactly ONE failure direction it is allowed to take (a token outlives its
# usefulness) and one it must never take (a live container locked out), and the
# whole class of bugs here is ECS reporting per-resource failures INSIDE a 200 —
# partial data that no `except` sees and that reads as positive proof of
# absence. Every such path raises with this suffix so the log says which.
_INCOMPLETE_NOTE = " — incomplete ECS data, keeping the rows"

# Page cap for the ListTasks enumeration (100 ARNs/page). ECS bounds a service's
# stopped-task history far below this; blowing through it means the enumeration
# is not terminating, which is unknown, not empty.
_MAX_TASK_PAGES = 50


def _tokens_table_name() -> str:
    override = os.environ.get("BEDROCK_GATE_TOKENS_TABLE")
    if override:
        return override
    env = settings.ENVIRONMENT or "dev"
    return f"isol8-{env}-bedrock-gate-tokens"


def _tokens_table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_tokens_table_name())


def hash_token(token: str) -> str:
    """SHA-256 hex of the plaintext token — the gate-tokens table PK.

    MUST match the gateway Lambda's ``hashToken`` (``node:crypto`` SHA-256 hex of
    the raw bearer string) so a token minted here resolves there.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _owner_service_view(owner_id: str) -> tuple[Any, str, str]:
    """``(ecs client, cluster arn, service name)`` for the owner's container.

    Goes through the ``EcsManager`` singleton so the ECS wiring and the
    service-name derivation keep exactly one definition. Imported lazily —
    ``ecs_manager`` imports this module (also lazily) and a module-scope import
    here would close the cycle.
    """
    from core.containers import get_ecs_manager

    mgr = get_ecs_manager()
    return mgr._ecs, mgr._cluster, mgr._service_name(owner_id)


def _refers_to(identifier: str | None, service_name: str) -> bool:
    """True when an ECS-returned identifier names the service we asked about.

    DescribeServices echoes back either the bare name we passed (``serviceName``,
    and the ``arn`` of a MISSING failure when it could not resolve one) or a
    full ARN, so compare on the last path segment.
    """
    return bool(identifier) and identifier.rsplit("/", 1)[-1] == service_name


async def _task_def_gate_token(ecs: Any, task_def_arn: str) -> str | None:
    """The gate bearer token baked into a task definition's env, or None.

    None means "this definition carries no gate token" — a real answer for a
    non-Bedrock owner, or for break-glass direct mode where no gate URL
    resolves. An INCOMPLETE response is not that answer: every registered task
    definition has at least one container definition, so a reply missing them
    is missing data, and reading it as "no token" would help delete a live row.
    Raise instead — the caller keeps the rows (see ``_INCOMPLETE_NOTE``).
    """
    resp = await run_in_thread(ecs.describe_task_definition, taskDefinition=task_def_arn)
    containers = (resp.get("taskDefinition") or {}).get("containerDefinitions")
    if not containers:
        raise RuntimeError(
            f"DescribeTaskDefinition returned no container definitions for {task_def_arn}{_INCOMPLETE_NOTE}"
        )
    for container in containers:
        for env in container.get("environment", []):
            if env.get("name") == _GATE_TOKEN_ENV:
                return env.get("value")
    return None


async def _live_task_definition_arns(ecs: Any, cluster: str, service_name: str) -> list[str]:
    """Task-definition ARNs of the service's tasks that have not stopped yet.

    Both desired states are listed because a task on its way out shows
    ``desiredStatus=STOPPED`` before ``lastStatus`` reaches STOPPED. The filter
    is ``lastStatus != "STOPPED"``, so it admits DEACTIVATING / STOPPING /
    DEPROVISIONING as well as RUNNING — a terminating task is kept because it
    may still be mid-request, and keeping is the safe direction.

    This is NOT a dead branch (an earlier comment claimed it was, reading
    "no RUNNING task with desiredStatus=STOPPED in prod" as "nothing is ever
    listed here"). A STOPPING task has ``desiredStatus=STOPPED`` by definition,
    and ``runningCount``/``pendingCount`` do not count a DEPROVISIONING task, so
    ECS can start the replacement while the old one tears down. What makes it
    inert is WHEN we evaluate — see ``_presentable_gate_token_hashes``.

    An INCOMPLETE enumeration raises rather than returning a short list, on both
    halves — a missing ARN here reads downstream as "no task presents that
    token", which is the deletion decision.
    """
    task_arns: list[str] = []
    for desired_status in ("RUNNING", "STOPPED"):
        # PAGINATE. ListTasks returns at most 100 ARNs plus a nextToken, in NO
        # guaranteed order, so a crash-looping service with a long stopped-task
        # history can push the one still-terminating task that proves the token
        # presentable onto a later page. Reading page 1 as the whole answer
        # deletes it.
        next_token: str | None = None
        for _ in range(_MAX_TASK_PAGES):
            kwargs = {"cluster": cluster, "serviceName": service_name, "desiredStatus": desired_status}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = await run_in_thread(ecs.list_tasks, **kwargs)
            task_arns.extend(resp.get("taskArns") or [])
            next_token = resp.get("nextToken")
            if not next_token:
                break
        else:
            # Still paging past the cap: the enumeration is unfinished, and an
            # unfinished list is unknown, not empty.
            raise RuntimeError(
                f"ListTasks for {service_name} ({desired_status}) exceeded {_MAX_TASK_PAGES} pages{_INCOMPLETE_NOTE}"
            )

    arns: list[str] = []
    # DescribeTasks caps at 100 ids per call. Chunk rather than truncate — the
    # STOPPED-desired list is mostly stopped history, and ListTasks promises no
    # ordering, so an arbitrary cap would silently decide which tasks count.
    for offset in range(0, len(task_arns), 100):
        chunk = task_arns[offset : offset + 100]
        resp = await run_in_thread(ecs.describe_tasks, cluster=cluster, tasks=chunk)
        tasks = resp.get("tasks") or []
        # ECS reports per-resource problems in `failures` INSIDE a 200, so a
        # short `tasks` list is not an exception and not an answer. If the
        # dropped ARN was an unstopped task on the superseded definition, the
        # comprehension below would quietly conclude nothing presents that
        # token.
        if resp.get("failures") or {t.get("taskArn") for t in tasks} != set(chunk):
            raise RuntimeError(
                f"DescribeTasks returned {len(tasks)} of {len(chunk)} tasks for {service_name}{_INCOMPLETE_NOTE}"
            )
        arns.extend(
            task["taskDefinitionArn"]
            for task in tasks
            if task.get("lastStatus") != "STOPPED" and task.get("taskDefinitionArn")
        )
    return arns


async def _presentable_gate_token_hashes(owner_id: str) -> dict[str, str]:
    """Hashes of every gate token the owner's container can still present,
    mapped to WHY -- which half of the predicate holds it alive. That reason
    is what makes the expiry decision log a diagnosis instead of a verdict.

    THE PREDICATE. Two sources, and in this fleet only the first one decides
    anything:

    1. the ECS **service's current task definition** — what the service will
       launch next. A drained service running nothing still points at it, so
       this is what keeps a token alive across scale-to-zero, a crash/restart,
       and (the incident) a redeploy that never landed. **This is the half that
       does the work today.**
    2. **live tasks** — anything ECS has not finished stopping, on whatever
       definition it launched from. Reachable (see
       ``_live_task_definition_arns``), but inert AT THE EVALUATION POINTS: the
       roll-confirmation call fires once the NEW task is RUNNING, by which time
       single-writer has long since stopped the old one, and the mint-time
       backstop runs when the service is scaled to zero. So in practice every
       ARN it contributes is already the service's own definition. It is not
       "forbidden by ``SINGLE_WRITER_DEPLOYMENT_CONFIG``" — that config bars
       old and new tasks from RUNNING together, not a terminating task from
       being listed — and it becomes load-bearing the moment anyone raises
       ``maximumPercent`` above 100 or evaluates mid-teardown.

    Answering from running tasks alone would be WORSE than the TTL it replaces.
    During the incident's reprovision the service was already drained to zero
    when the new token was minted, so that rule would have deleted the live
    token on the spot instead of an hour later.

    Raises on any ECS failure — the caller turns that into "keep the row".

    Returning ``{}`` DELETES every tracked row, so it is only ever returned
    from a POSITIVE finding that this owner has no service: an ``INACTIVE``
    status, or an explicit ``MISSING`` failure for the identifier we asked
    about. Never from missing data.

    That distinction is the whole safety property, and it is easy to lose
    because **ECS reports per-resource failures inside a SUCCESSFUL response**,
    not as an exception — so a broad ``except`` cannot be the fail-open
    mechanism. Partial data must be turned into a raise explicitly, on every
    call: a service neither described nor reported MISSING, a live service with
    no ``taskDefinition``, an unfinished ``ListTasks`` pagination, a
    ``DescribeTasks`` chunk that came back short or with failures, and a
    ``DescribeTaskDefinition`` with no container definitions. Each of those
    otherwise reads as "nothing presents this token" and permanently deletes a
    credential the running container is using — ``UnrecognizedClientException``
    on its next inference call, the incident this module exists to prevent.
    """
    ecs, cluster, service_name = _owner_service_view(owner_id)

    resp = await run_in_thread(ecs.describe_services, cluster=cluster, services=[service_name])
    service = next(
        (
            s
            for s in resp.get("services") or []
            if _refers_to(s.get("serviceName") or s.get("serviceArn"), service_name)
        ),
        None,
    )
    if service is None:
        # ECS did not describe our service. Only an explicit MISSING failure for
        # the identifier we asked about PROVES it is gone — that is the genuine
        # deleted-owner case, and it must still expire. A failure with any other
        # reason, or a response carrying neither our service nor a failure for
        # it, is unknown: ECS reports per-resource problems in `failures` inside
        # a 200, so no exception fires and the broad except never runs.
        if any(
            f.get("reason") == "MISSING" and _refers_to(f.get("arn"), service_name) for f in resp.get("failures") or []
        ):
            # Nothing will launch, and there is no service left to enumerate
            # tasks for.
            return {}
        raise RuntimeError(
            f"DescribeServices did not resolve {service_name}: failures={resp.get('failures')}{_INCOMPLETE_NOTE}"
        )
    if service.get("status") == "INACTIVE":
        # Deleted but still described for a while. Also positive absence.
        return {}

    service_task_def = service.get("taskDefinition")
    if not service_task_def:
        # DescribeServices on a live service ALWAYS carries taskDefinition, so
        # this is incomplete data, not "the service launches nothing". Treating
        # it as the latter is the one shape that would delete a token the
        # container is holding — the exact class the liveness predicate exists
        # to eliminate. Raise; the caller keeps the rows.
        raise RuntimeError(
            f"ECS service {service_name} is {service.get('status')} but reports no task definition{_INCOMPLETE_NOTE}"
        )
    service_def_arns: list[str] = [service_task_def]
    live_arns = await _live_task_definition_arns(ecs, cluster, service_name)

    hashes: dict[str, str] = {}
    # de-dupe, preserving the service def first -- an arn that is both the
    # service's def and a live task's def is resolved once, and gets the
    # authoritative reason.
    for arn in dict.fromkeys(service_def_arns + live_arns):
        token = await _task_def_gate_token(ecs, arn)
        if not token:
            continue
        # NOT "a task is still running with it": the live half admits
        # DEACTIVATING/STOPPING/DEPROVISIONING too, and asserting "running"
        # about a task that is shutting down is exactly the kind of overclaim
        # these reason strings exist to avoid.
        reason = (
            "service task definition still references it"
            if arn in service_def_arns
            else "a task launched from it has not stopped yet"
        )
        hashes.setdefault(hash_token(token), reason)
    return hashes


async def _disarm_ttl(table, token_hash: str) -> None:
    """REMOVE any legacy ``ttl`` from a token row we are about to protect.

    THE INVARIANT: no token row anything can present may carry a clock-based
    expiry. This module replaced a flat one-hour TTL with a liveness predicate,
    but deleting the constant does not delete the ``ttl`` attributes already
    written onto rows — each is a live clock still counting down, and DynamoDB
    will reap the row out from under the container presenting it. That is the
    exact lockout this branch exists to prevent, and without this the branch
    could REPRODUCE it on deploy.

    It also makes the rolling deploy safe: an old backend task keeps minting
    with the old behaviour and TTLs the outgoing row, the new code tracks that
    hash, and tracking it disarms it.

    Conditional on the attribute existing, so a row without one takes no write —
    which matters because ``_retain`` calls this on a REPEATED path.

    Never raises, and the caller does not check an outcome: a throttle or 5xx
    here is retried by the next retention, so aborting the mint would fail a
    provision over a condition that fixes itself. But a failure must never read
    as a success — the WARNING says the clock is STILL ARMED, and only a write
    that actually landed logs "disarmed". WARNING rather than ERROR because the
    next liveness evaluation re-attempts it; a sustained DynamoDB fault is
    alarmed on its own.
    """
    try:
        await run_in_thread(
            table.update_item,
            Key={"token_hash": token_hash},
            UpdateExpression="REMOVE #ttl",
            ConditionExpression="attribute_exists(token_hash) AND attribute_exists(#ttl)",
            ExpressionAttributeNames={"#ttl": "ttl"},
        )
        logger.info("bedrock-gate token %s.. legacy ttl disarmed", token_hash[:_HASH_PREFIX_LEN])
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return  # no ttl (or no row) — the invariant already holds
        _log_still_armed(token_hash)
    except Exception:  # noqa: BLE001 — never break a mint over bookkeeping
        _log_still_armed(token_hash)


def _log_still_armed(token_hash: str) -> None:
    logger.warning(
        "bedrock-gate token %s.. ttl removal FAILED — the legacy clock is STILL ARMED on a row we are "
        "retaining; DynamoDB may reap it out from under the container presenting it. Re-attempted on the "
        "next liveness evaluation.",
        token_hash[:_HASH_PREFIX_LEN],
        exc_info=True,
    )


async def _drain_superseded(
    owner_id: str, tracked: list[str], *, current_hash: str | None, adopt_orphans: bool = False
) -> list[str]:
    """Delete each tracked hash nothing can present; return the survivors.

    Deliberately does NOT write the survivors back to the containers row: that
    list is ``swap_gate_token_hash``'s alone (``container_repo`` refuses it from
    any other writer), and a survivor set computed here would be stale by the
    time it landed anyway. ``mint_gate_token`` prunes the list instead, inside
    the conditional swap it already makes; until then a hash that has already
    been deleted just re-resolves to a no-op delete.

    Every hash it RETAINS — kept, adopted, or held because the liveness check
    itself failed — has any legacy ``ttl`` re-asserted away first; see
    ``_retain`` for why that has to happen on retention and not on adoption.

    ``adopt_orphans`` — also return any hash the PREDICATE says is presentable
    that is neither the current pointer nor already tracked. That is the legacy
    failed-roll state this whole module was written about: the service's task
    definition presents A, the row points at B, and nothing records A, so
    nothing protects it or disarms its one-hour ``ttl``. Adopting it makes it an
    ordinary tracked hash — protected by the predicate, covered by
    ``deny_gate_token``, expired only when nothing can present it.

    OFF by default, and that is a COST decision, not a correctness one: adopting
    requires the ECS view, so it cannot share the empty-list early return below.
    ``expire_superseded_gate_tokens`` runs on every wake for every owner and
    must keep that early return; ``mint_gate_token`` runs once per provision, a
    path already making far more AWS calls than this, so it adopts.
    """
    candidates = [h for h in tracked if h and h != current_hash]
    if not candidates and not adopt_orphans:
        return []

    table = _tokens_table()
    try:
        presentable = await _presentable_gate_token_hashes(owner_id)
    except Exception:
        # Fail toward availability: if we cannot prove the token is dead, it lives.
        logger.warning(
            "bedrock-gate token liveness check failed for owner %s; keeping %d superseded row(s)",
            owner_id,
            len(candidates),
            exc_info=True,
        )
        return await _retain(table, candidates)

    survivors: list[str] = []
    if adopt_orphans:
        known = {current_hash, *candidates}
        for orphan in presentable:
            if orphan not in known:
                logger.warning(
                    "bedrock-gate token %s.. ADOPTED for owner %s: the service presents it but nothing tracked it "
                    "(legacy failed-roll state) — %s",
                    orphan[:_HASH_PREFIX_LEN],
                    owner_id,
                    presentable[orphan],
                )
                survivors.append(orphan)
    for token_hash in candidates:
        reason = presentable.get(token_hash)
        if reason is not None:
            logger.info(
                "bedrock-gate token %s.. kept for owner %s: %s",
                token_hash[:_HASH_PREFIX_LEN],
                owner_id,
                reason,
            )
            survivors.append(token_hash)
            continue
        try:
            # ALL_OLD so the "expired" line fires exactly ONCE, on the
            # evaluation that actually removed the row. The survivor list is not
            # written back (see the docstring), so a hash stays tracked until
            # the next mint and every wake in between re-evaluates it and
            # re-issues this no-op delete — logging each of those as an expiry
            # would make N re-confirmations indistinguishable from N expiries,
            # which is the one question these logs exist to answer.
            deleted = await run_in_thread(table.delete_item, Key={"token_hash": token_hash}, ReturnValues="ALL_OLD")
        except Exception:
            logger.warning(
                "failed to delete superseded bedrock-gate token row for owner %s",
                owner_id,
                exc_info=True,
            )
            survivors.append(token_hash)
            continue
        if deleted.get("Attributes"):
            logger.info(
                "bedrock-gate token %s.. expired for owner %s: no task definition or unstopped task presents it",
                token_hash[:_HASH_PREFIX_LEN],
                owner_id,
            )
    return await _retain(table, survivors)


async def _retain(table, hashes: list[str]) -> list[str]:
    """Record these hashes as still-live, re-asserting that none carries a ``ttl``.

    THE INVARIANT, stated the way it has to be enforced: *a ``ttl`` on a row we
    are RETAINING is never legitimate, so every retention re-asserts its
    absence.* Not "remove it when we adopt" — adoption happens once, and a
    one-shot write loses to anything that arms the clock afterwards:

    - a rolling deploy where an old task moves the pointer, pauses, and only
      then issues its unconditional ``SET ttl`` on the outgoing hash — after
      the new task has already looked and found nothing to remove;
    - a disarm that was throttled or 5xx'd, which no amount of care at the
      moment of adoption can retry.

    Retention is evaluated on every drain, so making it the place the invariant
    is asserted heals both without having to tell them apart.
    """
    for token_hash in hashes:
        await _disarm_ttl(table, token_hash)
    return hashes


async def expire_superseded_gate_tokens(owner_id: str) -> None:
    """Delete the owner's superseded gate-token rows IFF nothing can present them.

    Call this wherever a redeploy is CONFIRMED to have landed (the container now
    holds the current token) and as a backstop at mint time. The predicate is the
    correctness; the call site is only scheduling — a skipped call leaves a stale
    row, which is safe, and an extra call is a no-op.

    NEVER RAISES. Non-Bedrock owners (``chatgpt_oauth`` / ``byo_key``) have no
    token and no tracked hashes, so this returns before touching ECS at all.
    """
    try:
        row = await container_repo.get_by_owner_id(owner_id)
        tracked = list((row or {}).get(_SUPERSEDED_FIELD) or [])
        if not tracked:
            return
        await _drain_superseded(owner_id, tracked, current_hash=(row or {}).get("bedrock_gate_token_hash"))
    except Exception:
        logger.warning("expire_superseded_gate_tokens failed for owner %s", owner_id, exc_info=True)


async def mint_gate_token(owner_id: str, aip_arns: dict[str, str]) -> str:
    """Mint (rotating) a per-owner bedrock-gate bearer token; return the PLAINTEXT.

    - 32 random bytes, urlsafe-base64 (``secrets.token_urlsafe(32)``).
    - Writes a hash-keyed row ``{token_hash, owner_id, denied=False,
      aip_tiers={arn: tier}, created_at}``. ``aip_tiers`` is the INVERSE of the
      owner's ``{tier: arn}`` map — the gateway keys authorization + pricing on
      the AIP ARN.
    - Points the containers row's ``bedrock_gate_token_hash`` at the new hash,
      through ``container_repo.swap_gate_token_hash`` — a conditional write, so
      a concurrent mint cannot silently overwrite this one's hash; it is either
      merged or reported (see ``_swap_hash_pointer``). Still a no-op on a
      not-yet-created row; ``persist_gate_token_hash`` re-runs the same swap
      once ``create_user_service``'s upsert has created it.
    - ROTATION SCHEDULES NOTHING. The superseded hash is recorded in
      ``_SUPERSEDED_FIELD`` and its row survives until the liveness predicate
      proves no task definition and no live task can present it. The mint first
      runs that drain as a backstop for a confirmation that never came, then
      writes the pruned survivors plus the outgoing hash in the SAME row update.

    CONCURRENCY. The mint block in ``provision_user_container`` sits AHEAD of
    the first ``_provision_checkpoint``, so the provision lease does not
    serialize it: a poll provision and an escalating ``container_recover``
    takeover can both reach here at once. The row write is therefore conditioned
    on the hash this call read, and a lost race is RETRIED (bounded — see
    ``_swap_hash_pointer``) after folding the winner's hash, read off the
    refused write itself, into the superseded list. Dropping a hash would strand a ``denied=False``
    token row that nothing can reach: unfindable (hash-keyed table, no owner
    index), unexpirable, and outside ``deny_gate_token``'s reach. Exhausting
    the bound leaves exactly that gap, so it logs at ERROR.

    Only the hash is stored anywhere; the returned plaintext is the sole copy.
    """
    prior = await container_repo.get_by_owner_id(owner_id)
    prior_hash = prior.get("bedrock_gate_token_hash") if prior else None
    tracked = list(prior.get(_SUPERSEDED_FIELD) or []) if prior else []

    # Backstop pass: anything superseded by an EARLIER mint whose roll was never
    # confirmed. At this point the service still references the pre-mint task
    # definition, so the token the container is actually holding is presentable
    # and survives — only genuinely orphaned rows go.
    #
    # ADOPTING here too (the once-per-provision path, which already makes the
    # ECS calls this needs) is what heals an owner deployed into from the legacy
    # state: task definition presents A, pointer is B, nothing tracks A, and A's
    # row still carries the one-hour ttl the old code wrote.
    superseded = await _drain_superseded(owner_id, tracked, current_hash=prior_hash, adopt_orphans=True)

    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    # Invert {tier: arn} -> {arn: tier}: the gateway looks a request's model-id
    # (an AIP ARN) up in this map to authorize + price it.
    aip_tiers = {arn: tier for tier, arn in (aip_arns or {}).items()}

    table = _tokens_table()
    await run_in_thread(
        table.put_item,
        Item={
            "token_hash": token_hash,
            "owner_id": owner_id,
            "denied": False,
            "aip_tiers": aip_tiers,
            "created_at": utc_now_iso(),
        },
    )

    if prior_hash and prior_hash != token_hash and prior_hash not in superseded:
        superseded.append(prior_hash)

    # Disarm the OUTGOING pointer specifically. Everything else in `superseded`
    # came through the drain, which disarms what it retains; this one cannot
    # have, because at drain time it was still `current_hash` and the drain
    # excludes that by construction (you must never expire the live token). It
    # is also the hash a mid-rollout old task is most likely to have just
    # TTL'd, since superseding it is exactly what the old code TTL'd on.
    # One-shot, and it does not need to be more: from here on it is tracked, so
    # every later retention re-asserts it.
    if prior_hash and prior_hash in superseded:
        await _disarm_ttl(table, prior_hash)

    await _swap_hash_pointer(owner_id, token_hash, superseded=superseded, expected_prior_hash=prior_hash)

    logger.info(
        "bedrock-gate token minted for owner %s: new=%s.. prior=%s tiers=%s",
        owner_id,
        token_hash[:_HASH_PREFIX_LEN],
        (prior_hash[:_HASH_PREFIX_LEN] + "..") if prior_hash else "none",
        ",".join(sorted(aip_arns)) if aip_arns else "none",
    )
    return token


async def persist_gate_token_hash(
    owner_id: str,
    token_hash: str,
    *,
    claim_id: str | None = None,
    attempt_token: str | None = None,
) -> None:
    """Re-assert the row's pointer at ``token_hash`` once the row exists.

    ``mint_gate_token`` runs BEFORE ``create_user_service``, so on any path
    where the containers row is not there yet its swap answers ``"absent"`` and
    writes nothing — leaving a ``denied=False`` token row reachable from
    nowhere. ``create_user_service`` calls this immediately after its
    row-creating upsert, which is the first moment there is a row to condition
    on. Through the SAME conditional swap, deliberately: a whole-item put here
    is precisely the read-to-write race that made the row's pointer clobberable
    in the first place.

    A no-op in the ordinary case, where the row already existed at mint time
    (``try_claim_provision``/``takeover_provision`` create it before the
    provision runs) and the mint's own swap already landed this hash.

    ``claim_id``/``attempt_token`` put the caller's provision lease INTO the
    swap's condition, so a run superseded between its last checkpoint and this
    write is refused atomically rather than pointing the row at a token no task
    of its will ever present. Raises ``ProvisionSuperseded`` when that happens;
    the caller must stop. Omitted for callers that hold no lease.
    """
    row = await container_repo.get_by_owner_id(owner_id) or {}
    prior_hash = row.get("bedrock_gate_token_hash")
    if prior_hash == token_hash:
        return
    tracked = [*(row.get(_SUPERSEDED_FIELD) or []), prior_hash]
    superseded = [h for h in dict.fromkeys(tracked) if h and h != token_hash]
    await _swap_hash_pointer(
        owner_id,
        token_hash,
        superseded=superseded,
        expected_prior_hash=prior_hash,
        claim_id=claim_id,
        attempt_token=attempt_token,
    )


async def _swap_hash_pointer(
    owner_id: str,
    token_hash: str,
    *,
    superseded: list[str],
    expected_prior_hash: str | None,
    claim_id: str | None = None,
    attempt_token: str | None = None,
) -> None:
    """Conditional swap of the row's gate-token pointer, with bounded retry.

    Never mints, and a lost race is logged rather than raised: provisioning must
    not fail over a bookkeeping write. Each retry re-runs the WRITE only (a
    re-mint per attempt would leak exactly the unreachable token row this exists
    to prevent), re-armed from the state DynamoDB returned for the refused write.

    THREE attempts, no backoff. Each is one UpdateItem against a
    strongly-consistent condition, so an attempt can only fail again if ANOTHER
    write moved the hash in between, never because a replica was stale —
    sleeping would just widen that window.

    Why three: every interfering write to ``bedrock_gate_token_hash`` costs at
    most one attempt, and a rival provision makes exactly ONE — its own
    ``mint_gate_token`` swap. Its later ``persist_gate_token_hash`` re-assert
    does not write at all, since the pointer already equals the hash that swap
    wrote. The shape this path actually has is a poll provision racing a
    ``container_recover`` takeover, so 1 + 1 = 2 suffices and the third is
    spare. N concurrent minters would want N + 1, but nothing produces them
    today, and exhaustion is a LOGGED leak, not a lockout.

    A lease that moves is NOT retried — ``swap_gate_token_hash`` raises
    ``ProvisionSuperseded`` straight through this loop, because retrying would
    be the loser re-arming against the winner's row and writing anyway.
    """
    expected = expected_prior_hash
    outcome, conflicting = "conflict", None
    for _ in range(3):
        outcome, conflicting = await container_repo.swap_gate_token_hash(
            owner_id,
            new_hash=token_hash,
            superseded=superseded,
            expected_prior_hash=expected,
            claim_id=claim_id,
            attempt_token=attempt_token,
        )
        if outcome != "conflict":
            break
        if not conflicting:
            # Conflicted but the prior row could not be parsed (see
            # swap_gate_token_hash's guard). Re-arming from nothing would ask
            # to write an EMPTY superseded list, dropping every tracked hash.
            # Stop here instead and fall into the ERROR log below.
            break
        # A concurrent mint got there first. Its hash is now the row's current
        # one and NOTHING else records it, so carry it into our superseded list
        # or it becomes unreachable. Order-preserving, de-duplicated, and never
        # including our own new hash.
        expected = conflicting.get("bedrock_gate_token_hash")
        merged = [*(conflicting.get(_SUPERSEDED_FIELD) or []), expected]
        superseded = [h for h in dict.fromkeys(merged) if h and h != token_hash]
    if outcome == "conflict":
        # Do NOT raise: provisioning must not fail over this. The failure mode
        # is the tracking gap we already had before the retry existed.
        logger.error(
            "bedrock-gate token hash swap lost 3 races for owner %s: new=%s.. expected-prior=%s "
            "— the row still points elsewhere and this token row is untracked",
            owner_id,
            token_hash[:_HASH_PREFIX_LEN],
            (expected[:_HASH_PREFIX_LEN] + "..") if expected else "none",
        )


def owner_gate_token_hashes(row: dict | None) -> list[str]:
    """Every gate-token hash a containers ``row`` still names — the current
    pointer plus each tracked superseded hash, order-preserving and de-duped.

    The gate-tokens table is keyed on the hash and has no owner index, so this
    list is the ONLY handle on an owner's rows. One definition of the set, used
    by everything that has to cover all of it: ``deny_gate_token`` below, and
    the account wipe (``core.services.account_wipe``), which deletes exactly
    this set before the row that names it is destroyed.
    """
    row = row or {}
    hashes = [row.get("bedrock_gate_token_hash"), *(row.get(_SUPERSEDED_FIELD) or [])]
    return list(dict.fromkeys(h for h in hashes if h))


async def delete_gate_token_rows(token_hashes: list[str]) -> int:
    """Delete these gate-token rows outright; return how many existed.

    For account teardown ONLY — the ordinary rotation path never deletes a row
    without the liveness predicate's permission (``_drain_superseded``). Here
    the whole account is going, so there is no container left to lock out, and
    NOT deleting is the harmful direction: once the containers row naming these
    hashes is gone, the rows can never be found, expired, or denied again (four
    were stranded that way in prod, all at ``denied=False``).

    Every hash is attempted even if one delete fails, and the first failure is
    re-raised so the caller's step reports it rather than claiming success.
    """
    table = _tokens_table()
    deleted = 0
    failure: Exception | None = None
    for token_hash in token_hashes:
        try:
            resp = await run_in_thread(table.delete_item, Key={"token_hash": token_hash}, ReturnValues="ALL_OLD")
        except Exception as e:  # noqa: BLE001 — try every hash, raise the first failure
            failure = failure or e
            continue
        if resp.get("Attributes"):
            deleted += 1
    if failure is not None:
        raise failure
    return deleted


async def deny_gate_token(owner_id: str, denied: bool) -> None:
    """Flip the ``denied`` flag on EVERY gate-token row the owner can present.

    That is the current hash (``bedrock_gate_token_hash``) **plus every hash
    still tracked in** ``_SUPERSEDED_FIELD`` — not the current one alone. Since
    a superseded row now lives exactly as long as something can still present
    it, denying only the current hash would defeat the kill switch in precisely
    the state this module preserves: mint B, the roll never lands, the service's
    task definition still carries A, the container does all its inference with
    A — and the deny lands on B, a row nothing presents. Under the TTL this
    replaced, A was reaped within an hour; here it lives indefinitely, so the
    coverage has to follow it.

    The set is the same one ``_drain_superseded`` judges, so a denied token can
    never outlive its tracking: expiry deletes the row outright.

    NO-OP when the owner has no hashes at all (nothing to deny) — callers
    (``revoke_owner_bedrock`` / ``restore_owner_bedrock``, ``ecs_manager``'s
    revoked-owner re-assert) treat this as a best-effort backstop alongside the
    IAM revoke, so the absence of a token must be silent, not an error. A hash
    whose row has already been deleted is likewise silent: the write is
    conditioned on the row existing, so a kill never RESURRECTS a dead token as
    a partial row (and a restore never resurrects one as ``denied=false``).

    Every hash is attempted even if one write fails — a kill switch must not
    skip the rest of the set — and the first failure is re-raised afterwards so
    callers still see it.
    """
    row = await container_repo.get_by_owner_id(owner_id) or {}
    hashes = owner_gate_token_hashes(row)
    if not hashes:
        return
    table = _tokens_table()
    failure: Exception | None = None
    for token_hash in hashes:
        try:
            await run_in_thread(
                table.update_item,
                Key={"token_hash": token_hash},
                UpdateExpression="SET #d = :d",
                ConditionExpression="attribute_exists(token_hash)",
                ExpressionAttributeNames={"#d": "denied"},
                ExpressionAttributeValues={":d": bool(denied)},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue  # already expired — nothing left to deny
            failure = failure or e
        except Exception as e:  # noqa: BLE001 — try every hash, raise the first failure
            failure = failure or e
    if failure is not None:
        raise failure
