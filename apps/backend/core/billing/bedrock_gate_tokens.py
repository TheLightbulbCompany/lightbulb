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
# Self-bounding, which is why it needs no cap or sweep: only ``mint_gate_token``
# appends to it, and it appends the pruned survivor set — and a hash only
# survives a prune while something can still present it, which is at most the
# service's task definition plus whatever tasks are live.
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


async def _task_def_gate_token(ecs: Any, task_def_arn: str) -> str | None:
    """The gate bearer token baked into a task definition's env, if any."""
    resp = await run_in_thread(ecs.describe_task_definition, taskDefinition=task_def_arn)
    for container in resp.get("taskDefinition", {}).get("containerDefinitions", []):
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
    """
    task_arns: list[str] = []
    for desired_status in ("RUNNING", "STOPPED"):
        resp = await run_in_thread(
            ecs.list_tasks,
            cluster=cluster,
            serviceName=service_name,
            desiredStatus=desired_status,
        )
        task_arns.extend(resp.get("taskArns", []))

    arns: list[str] = []
    # DescribeTasks caps at 100 ids per call. Chunk rather than truncate — the
    # STOPPED-desired list is mostly stopped history, and ListTasks promises no
    # ordering, so an arbitrary cap would silently decide which tasks count.
    for offset in range(0, len(task_arns), 100):
        resp = await run_in_thread(ecs.describe_tasks, cluster=cluster, tasks=task_arns[offset : offset + 100])
        arns.extend(
            task["taskDefinitionArn"]
            for task in resp.get("tasks", [])
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
    from a positive finding ("this owner has no service"), never from missing
    data — a live service that reports no ``taskDefinition`` raises instead.
    """
    ecs, cluster, service_name = _owner_service_view(owner_id)

    resp = await run_in_thread(ecs.describe_services, cluster=cluster, services=[service_name])
    live_services = [s for s in resp.get("services", []) if s.get("status") != "INACTIVE"]
    if not live_services:
        # No service (deleted owner, or never created). Nothing will launch, and
        # there is no service left to enumerate tasks for.
        return {}

    service = live_services[0]
    service_task_def = service.get("taskDefinition")
    if not service_task_def:
        # DescribeServices on a live service ALWAYS carries taskDefinition, so
        # this is incomplete data, not "the service launches nothing". Treating
        # it as the latter is the one shape that would delete a token the
        # container is holding — the exact class the liveness predicate exists
        # to eliminate. Raise; the caller keeps the rows.
        raise RuntimeError(f"ECS service {service_name} is {service.get('status')} but reports no task definition")
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


async def _drain_superseded(owner_id: str, tracked: list[str], *, current_hash: str | None) -> list[str]:
    """Delete each tracked hash nothing can present; return the survivors.

    Deliberately does NOT write the survivors back to the containers row.
    ``container_repo.update_fields`` is a get→put of the whole item, and this
    runs immediately after the claim-fenced ``status="running"`` write — a full
    put there could resurrect stale fields over it. ``mint_gate_token`` prunes
    the list instead, inside the row write it already makes; until then a
    hash that has already been deleted just re-resolves to a no-op delete.
    """
    candidates = [h for h in tracked if h and h != current_hash]
    if not candidates:
        return []

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
        return candidates

    table = _tokens_table()
    survivors: list[str] = []
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
    return survivors


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
    - Points the containers row's ``bedrock_gate_token_hash`` at the new hash.
      (No-ops on a not-yet-created row: the fresh-provision path folds the hash
      into ``create_user_service``'s row-creating upsert instead — see
      ``ecs_manager`` — mirroring how ``bedrock_role_arn`` / ``bedrock_aip_arns``
      are persisted.)
    - ROTATION SCHEDULES NOTHING. The superseded hash is recorded in
      ``_SUPERSEDED_FIELD`` and its row survives until the liveness predicate
      proves no task definition and no live task can present it. The mint first
      runs that drain as a backstop for a confirmation that never came, then
      writes the pruned survivors plus the outgoing hash in the SAME row update.

    Only the hash is stored anywhere; the returned plaintext is the sole copy.
    """
    prior = await container_repo.get_by_owner_id(owner_id)
    prior_hash = prior.get("bedrock_gate_token_hash") if prior else None
    tracked = list(prior.get(_SUPERSEDED_FIELD) or []) if prior else []

    # Backstop pass: anything superseded by an EARLIER mint whose roll was never
    # confirmed. At this point the service still references the pre-mint task
    # definition, so the token the container is actually holding is presentable
    # and survives — only genuinely orphaned rows go.
    superseded = await _drain_superseded(owner_id, tracked, current_hash=prior_hash)

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
    await container_repo.update_fields(
        owner_id,
        {"bedrock_gate_token_hash": token_hash, _SUPERSEDED_FIELD: superseded},
    )

    logger.info(
        "bedrock-gate token minted for owner %s: new=%s.. prior=%s tiers=%s",
        owner_id,
        token_hash[:_HASH_PREFIX_LEN],
        (prior_hash[:_HASH_PREFIX_LEN] + "..") if prior_hash else "none",
        ",".join(sorted(aip_arns)) if aip_arns else "none",
    )
    return token


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
    hashes = [h for h in [row.get("bedrock_gate_token_hash"), *(row.get(_SUPERSEDED_FIELD) or [])] if h]
    if not hashes:
        return
    table = _tokens_table()
    failure: Exception | None = None
    for token_hash in dict.fromkeys(hashes):
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
