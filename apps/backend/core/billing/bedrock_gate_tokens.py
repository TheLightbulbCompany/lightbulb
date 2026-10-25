"""Per-owner bedrock-gate bearer tokens — mint / expire / deny.

The bedrock-gate Lambda authenticates every inbound Bedrock request by
SHA-256-hashing the caller's ``Authorization: Bearer <token>`` and GetItem-ing a
row in the per-env gate-tokens table keyed on that hash. This module is the
backend WRITER of those rows.

Hash-only storage: the plaintext token exists exactly ONCE — at mint time, when
it is returned to the caller for the owner's boot secret, from which the runtime
exports ``AWS_BEARER_TOKEN_BEDROCK``. Nothing persists the
plaintext (mirrors ``api_key_repo`` / hook-token posture): a leaked table row
cannot be turned back into a usable token, and a compromised token is handled by
rotate (bump the row's ``gate_bearer_generation``; the plaintext is re-derivable, see
``derive_gate_bearer``) or deny (kill switch), never by reading it back.

The bearer is the openrouter-gate's auth (the ``bedrock`` names are a scar from the retired
Bedrock gate). A row carries no Bedrock ``aip_tiers`` map: without one the retired
bedrock-gate refuses every model the bearer names.

Table naming: the gate-tokens table is GATE-PRIVATE state, defined in the
bedrock-gate CDK stack (NOT database-stack), so — unlike
``metronome-fast-gate``, whose full name arrives as a CDK-injected env var —
the backend constructs it by convention from the env name, matching the gateway
Lambda's own ``TOKENS_TABLE`` and the ``lightbulb-{env}-``
scheme. An explicit ``BEDROCK_GATE_TOKENS_TABLE`` override wins (tests / manual
wiring).

Lifetime: a token row lives until NOTHING CAN PRESENT IT. The token reaches the
runtime through its ONE boot secret, read at cold start, so the DDB side and the
runtime side are coupled only by a boot that actually happens — a warm microVM
keeps whatever it read. Minting therefore schedules nothing; a superseded row is
deleted only once ``_presentable_gate_token_hashes`` confirms the owner's
AgentCore runtime no longer names a boot secret carrying it.

``deny_gate_token`` (the kill switch) covers the SAME set, current hash plus
tracked superseded hashes. It has to: a superseded token now outlives the mint
indefinitely *because* something can still present it, so denying only the
current hash would leave the presentable one live.

This replaced a flat one-hour TTL set at mint time, which killed a prod
container for good: the reprovision minted a new token, put the old one on a
60-minute fuse, and then the rotation never reached the runtime the workspace
actually boots from — so it kept presenting a credential that DynamoDB reaped
out from under it an hour later. Every evaluation here therefore fails toward
AVAILABILITY: any error leaves the row alone. A stale row is a token nobody
holds; a deleted live row is a customer with zero inference.

KNOWN CONSTRAINT — AgentCore rotation (Ruling M4-R16). A warm AgentCore session
caches the bearer it read from the boot secret at cold-start; there is no API to
enumerate an owner's warm sessions, so writing a new value to the boot secret does
not, by itself, make a running session start presenting it. A rotation on an
AgentCore workspace must therefore ALSO call ``StopRuntimeSession`` for the
owner's session (forcing the next invoke to cold-start and read the new value)
BEFORE the old token generation is revoked — this module's liveness predicate has
no way to see a warm session's cached credential at all. Task 17's rotation path
owes that call; this module only drains what ``GetAgentRuntime`` can already tell
it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os

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
# most the one bearer the owner's boot secret carries. (A swap that
# loses the race adds the winner's hash on top of that, but the winner's own
# list was pruned the same way, so the bound holds.)
_SUPERSEDED_FIELD = "bedrock_gate_superseded_hashes"

# The env var whose value the predicate hashes; it is injected for every Bedrock
# owner whenever a gate URL resolves.
_GATE_TOKEN_ENV = "AWS_BEARER_TOKEN_BEDROCK"

# Hash chars logged for correlation against the boot secret's own bearer -- long
# enough to be useful for that, short enough that a log reader can never
# reconstruct or brute-force the plaintext from it.
_HASH_PREFIX_LEN = 12

# Appended to every "the runtime answer was incomplete" RuntimeError. The predicate
# has exactly ONE failure direction it is allowed to take (a token outlives its
# usefulness) and one it must never take (a live runtime locked out), so every
# UNKNOWN has to raise rather than read as positive proof of absence. This suffix
# is what makes the log say which.
_INCOMPLETE_NOTE = " — incomplete runtime data, keeping the rows"


# The AgentCore runtime env var naming the boot secret -- NOT the bearer plaintext.
# M4-R4/R5 ruling: there is exactly ONE boot-secret mechanism (M1's, already shipped
# in apps/infra/hermes/bridge/__init__.py / bridge/sync.load_boot_secrets): one JSON
# secret `lightbulb/{env}/hermes/{owner_id}/boot` = {"hookToken","gatewayToken",
# "bedrockBearer"}, named by this one env var. A per-credential secret-name scheme
# was considered and explicitly rejected as a second, incompatible convention.
_BOOT_SECRET_ENV = "LIGHTBULB_SECRET_ID"
_BEARER_SECRET_KEY = "bedrockBearer"


def _tokens_table_name() -> str:
    override = os.environ.get("BEDROCK_GATE_TOKENS_TABLE")
    if override:
        return override
    # Route through the SAME prefix every other table uses. A hardcoded
    # brand literal here silently outlived the table rename: CDK moved the
    # table to the new prefix while this kept naming the old one, and the
    # break only surfaced once the old table was deleted.
    return f"{settings.DYNAMODB_TABLE_PREFIX}bedrock-gate-tokens"


def _tokens_table():
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(_tokens_table_name())


def hash_token(token: str) -> str:
    """SHA-256 hex of the plaintext token — the gate-tokens table PK.

    MUST match the gateway Lambda's ``hashToken`` (``node:crypto`` SHA-256 hex of
    the raw bearer string) so a token minted here resolves there.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def derive_gate_bearer(owner_id: str, row_id: str, generation: int = 0, agent_id: str | None = None) -> str:
    """The owner's gate bearer for this containers-row incarnation and ``generation``:
    deterministic, so every concurrent provision run for one owner derives the SAME bearer
    and writes the same boot secret, token row and pointer instead of racing to rotate.

    ``urlsafe_b64(HMAC-SHA256(key=GATE_BEARER_HMAC_KEY,
    msg="lightbulb-gate-bearer:v1:<owner_id>:<row_id>:<generation>"))``, padding stripped
    (the ``token_urlsafe(32)`` shape the random mint had). The key is the
    ``GATE_BEARER_HMAC_KEY`` string's UTF-8 bytes -- a DEDICATED secret
    (``lightbulb/{env}/gate_bearer_hmac_key``), never ``ENCRYPTION_KEY``: rotating that one
    must not silently change every bearer, and holding it must not let anyone derive them.

    ``row_id`` is the containers row's ``id``: every creator writes it ``if_not_exists``, so
    concurrent runs read back the same one, and teardown deletes the row, so a reprovision
    gets a new id and therefore a new bearer (a bearer copied out of the old runtime stays
    dead). Within one incarnation, rotation is an explicit bump of the row's
    ``gate_bearer_generation``. Rotating ``GATE_BEARER_HMAC_KEY`` changes every owner's NEXT
    derivation, not a live runtime's bearer.

    A derived bearer cannot be un-issued, so revocation stays where it always was: the
    token row's ``denied`` flag (``deny_gate_token``) and deleting the row.

    Refuses an empty key (no fallback to any other key) or row id: without the key every
    bearer is computable from public ids, and without the row id a teardown would no longer
    change it.
    """
    key = settings.GATE_BEARER_HMAC_KEY
    if not key:
        raise RuntimeError("GATE_BEARER_HMAC_KEY is not configured; refusing to derive a gate bearer")
    if not row_id:
        raise ValueError(f"no containers row id for {owner_id}; refusing to derive a gate bearer")
    msg = f"lightbulb-gate-bearer:v1:{owner_id}:{row_id}:{int(generation)}"
    if agent_id:  # a per-agent bearer; the owner-level message is byte-identical to before
        msg += f":agent:{agent_id}"
    msg = msg.encode()
    digest = hmac.new(key.encode("utf-8"), msg, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _describe_agent_runtime(runtime_arn: str) -> dict:
    """``GetAgentRuntime`` for one workspace's runtime. Sync -- run off ``run_in_thread``."""
    client = boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)
    return client.get_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])


def _secret_value(name: str) -> str | None:
    """Raw ``SecretString`` for a Secrets Manager secret name."""
    client = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    return client.get_secret_value(SecretId=name).get("SecretString")


async def _container_row(owner_id: str) -> dict | None:
    return await container_repo.get_by_owner_id(owner_id, consistent=True)


async def _agentcore_presentable_hashes(owner_id: str, runtime_arn: str) -> dict[str, str]:
    """Hashes an AgentCore runtime can still present, mapped to why.

    One runtime per workspace (report 04 risk 3), so this is one ``GetAgentRuntime`` call and

    The runtime's ``environmentVariables`` carry ONLY the boot secret's NAME
    (``LIGHTBULB_SECRET_ID``), never the bearer plaintext -- see ``_BOOT_SECRET_ENV``'s
    comment for the M4-R4/R5 ruling. So the live bearer is resolved by fetching that secret
    and reading its ``bedrockBearer`` key, not by reading the env var's value directly.

    Exceptions from ``GetAgentRuntime`` or ``GetSecretValue`` propagate on purpose -- an
    unreadable runtime or secret is UNKNOWN, not empty, and reading either as empty is the
    deletion decision (see ``_INCOMPLETE_NOTE``). A secret body that is not the expected
    ``{hookToken, gatewayToken, bedrockBearer}`` shape is the same kind of incomplete data
    and raises too, rather than being read as "no live bearer".
    """
    runtime = await run_in_thread(_describe_agent_runtime, runtime_arn)
    secret_id = (runtime.get("environmentVariables") or {}).get(_BOOT_SECRET_ENV)
    if not secret_id:
        # A positive finding: this runtime names no boot secret at all, so it cannot
        # present any bearer -- not "we couldn't tell".
        return {}
    raw = await run_in_thread(_secret_value, secret_id)
    if not raw:
        # secret_id resolved -- a named secret with an empty or absent SecretString is
        # incomplete data, not proof the runtime holds no bearer. Same class as malformed
        # JSON below; raise rather than reading it as positive absence.
        raise RuntimeError(f"boot secret {secret_id} for {owner_id} has no SecretString{_INCOMPLETE_NOTE}")
    try:
        boot = json.loads(raw)
        bearer = boot[_BEARER_SECRET_KEY] if isinstance(boot, dict) else None
    except ValueError as e:
        raise RuntimeError(f"boot secret {secret_id} for {owner_id} is not valid JSON{_INCOMPLETE_NOTE}") from e
    if not bearer:
        raise RuntimeError(f"boot secret {secret_id} for {owner_id} carries no {_BEARER_SECRET_KEY}{_INCOMPLETE_NOTE}")
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return {hash_token(bearer): f"agentcore runtime {runtime_id} boot secret {secret_id}"}


async def _presentable_gate_token_hashes(owner_id: str) -> dict[str, str]:
    """Hashes of every gate token the owner's runtime can still present, mapped to WHY.
    That reason is what makes the expiry decision log a diagnosis instead of a verdict.

    One runtime per workspace (report 04 risk 3), so the whole predicate is
    ``_agentcore_presentable_hashes``: one ``GetAgentRuntime`` plus the boot secret it names.
    The ECS half this used to also carry -- the service's current task definition plus every
    not-yet-stopped task's definition, each read for its baked ``AWS_BEARER_TOKEN_BEDROCK``
    -- is gone with the service.

    A row that names no runtime yet (mid-provision) presents nothing: that is a positive
    finding, not missing data. Everything else that is UNKNOWN raises -- see
    ``_agentcore_presentable_hashes`` and ``_INCOMPLETE_NOTE``: reading unknown as empty is
    the deletion decision, and deleting a credential the runtime is using is
    ``UnrecognizedClientException`` on its next inference call, the incident this module
    exists to prevent.
    """
    row = await _container_row(owner_id) or {}
    arn = row.get("agentcore_runtime_arn")
    if not arn:
        return {}
    return await _agentcore_presentable_hashes(owner_id, arn)


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
    owner_id: str,
    tracked: list[str],
    *,
    current_hash: str | None,
    adopt_orphans: bool = False,
    expire: bool = True,
) -> list[str]:
    """Delete each tracked hash nothing can present; return the survivors.

    ``expire=False`` (``mint_gate_token``) keeps every candidate: the predicate reads the boot
    secret, and on the reconcile path a mint that has just rewritten that secret would judge a
    warm session's old bearer unpresentable while the session still holds it. Deletion is left
    to ``expire_superseded_gate_tokens`` after a confirmed ``StopRuntimeSession``.

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
    failed-roll state this whole module was written about: the runtime's boot
    secret presents A, the row points at B, and nothing records A, so
    nothing protects it or disarms its one-hour ``ttl``. Adopting it makes it an
    ordinary tracked hash — protected by the predicate, covered by
    ``deny_gate_token``, expired only when nothing can present it.

    OFF by default, and that is a COST decision, not a correctness one: adopting
    requires the runtime view, so it cannot share the empty-list early return below.
    ``expire_superseded_gate_tokens`` runs after every confirmed session stop and
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
                    "bedrock-gate token %s.. ADOPTED for owner %s: the runtime presents it but nothing tracked it "
                    "(legacy failed-roll state) — %s",
                    orphan[:_HASH_PREFIX_LEN],
                    owner_id,
                    presentable[orphan],
                )
                survivors.append(orphan)
    for token_hash in candidates:
        reason = presentable.get(token_hash)
        if reason is None and not expire:
            reason = "kept until a confirmed session stop"
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
            # the next mint and every expiry in between re-evaluates it and
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
                "bedrock-gate token %s.. expired for owner %s: the runtime's boot secret does not carry it",
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

    - a concurrent provision where the loser moves the pointer, pauses, and only
      then issues its unconditional ``SET ttl`` on the outgoing hash — after
      the winner has already looked and found nothing to remove;
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

    Called ONLY where a session stop is CONFIRMED (``roll_runtime_image``'s stop
    branch): the next session reads the current boot secret, so nothing can
    present a superseded bearer any more. The mint never deletes. The predicate is the
    correctness; the call site is only scheduling — a skipped call leaves a stale
    row, which is safe, and an extra call is a no-op.

    NEVER RAISES. Non-Bedrock owners (``chatgpt_oauth`` / ``byo_key``) have no
    token and no tracked hashes, so this returns before touching AWS at all.
    """
    try:
        row = await container_repo.get_by_owner_id(owner_id)
        tracked = list((row or {}).get(_SUPERSEDED_FIELD) or [])
        if not tracked:
            return
        await _drain_superseded(owner_id, tracked, current_hash=(row or {}).get("bedrock_gate_token_hash"))
    except Exception:
        logger.warning("expire_superseded_gate_tokens failed for owner %s", owner_id, exc_info=True)


async def mint_gate_token(owner_id: str, *, row_id: str, generation: int = 0) -> str:
    """Ensure the per-owner bedrock-gate bearer for ``generation``; return the PLAINTEXT.

    - ``derive_gate_bearer(owner_id, row_id, generation)`` -- the same value on every run
      against one row incarnation, so a second run is a no-op below; a bumped generation or
      a new row (teardown + reprovision) rotates.
    - Writes a hash-keyed row ``{token_hash, owner_id, denied, created_at}``, CREATE-ONLY; ``denied`` is True when any hash the owner still tracks
      is denied (a trial cap / kill must survive a rotation): a row that already exists (an
      earlier or concurrent run) is left exactly as it is, including a ``denied`` flag.
    - Points the containers row's ``bedrock_gate_token_hash`` at the new hash,
      through ``container_repo.swap_gate_token_hash`` — a conditional write, so
      a concurrent mint cannot silently overwrite this one's hash; it is either
      merged or reported (see ``_swap_hash_pointer``). A no-op on a
      not-yet-created row (dispatch creates the row before the runner mints).
    - ROTATION SCHEDULES NOTHING. The superseded hash is recorded in
      ``_SUPERSEDED_FIELD`` and its row survives until the liveness predicate
      proves the owner's runtime cannot present it -- judged only after a confirmed
      session stop (``expire_superseded_gate_tokens``), never here: the mint runs on
      every reconcile, right before the boot secret is rewritten, and a warm session
      still presents the bearer it read at cold start. It retains (and adopts) instead,
      then writes the survivors plus the outgoing hash in the SAME row update.

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

    # Retention pass: keep every tracked hash (expire=False -- see the docstring) and
    # re-assert that none carries a legacy ttl. ADOPTING here too is what heals an owner
    # deployed into from the legacy state: the runtime presents A, the pointer is B,
    # nothing tracks A, and A's row still carries the one-hour ttl the old code wrote.
    superseded = await _drain_superseded(owner_id, tracked, current_hash=prior_hash, adopt_orphans=True, expire=False)

    token = derive_gate_bearer(owner_id, row_id, generation)
    token_hash = hash_token(token)

    table = _tokens_table()
    # A capped/killed owner stays denied across the rotation: the new row inherits a deny
    # from ANY hash the owner can still present, or the mint itself would lift the kill.
    denied = False
    for tracked_hash in dict.fromkeys([*owner_gate_token_hashes(prior), *superseded]):
        resp = await run_in_thread(table.get_item, Key={"token_hash": tracked_hash}, ConsistentRead=True)
        if (resp.get("Item") or {}).get("denied"):
            denied = True
            break
    try:
        await run_in_thread(
            table.put_item,
            Item={
                "token_hash": token_hash,
                "owner_id": owner_id,
                "denied": denied,
                "created_at": utc_now_iso(),
            },
            ConditionExpression="attribute_not_exists(token_hash)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise

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

    if prior_hash != token_hash:
        await _swap_hash_pointer(owner_id, token_hash, superseded=superseded, expected_prior_hash=prior_hash)

    logger.info(
        "bedrock-gate token minted for owner %s: new=%s.. prior=%s",
        owner_id,
        token_hash[:_HASH_PREFIX_LEN],
        (prior_hash[:_HASH_PREFIX_LEN] + "..") if prior_hash else "none",
    )
    return token


async def mint_agent_gate_token(owner_id: str, agent_id: str, *, row_id: str, generation: int = 0) -> str:
    """Ensure one roster agent's own gate bearer; return the PLAINTEXT (for that agent's profile).

    Our auth only: the gate resolves it to ``{owner_id, agent_id}`` but still uses the OWNER's one
    OpenRouter key and daily limit. Row is create-only ``{token_hash, owner_id, agent_id, denied,
    created_at}``; a new agent inherits a deny already on the owner's token. The hash is tracked on
    the containers row BEFORE the put (a tracked hash with no row is harmless; the reverse is an
    unreachable live bearer). ponytail: rotation (generation bump) leaves the old generation's agent
    rows tracked and undrained -- deny/wipe still reach them; add a drain when rotation is exercised.
    """
    token = derive_gate_bearer(owner_id, row_id, generation, agent_id=agent_id)
    token_hash = hash_token(token)
    await container_repo.add_agent_gate_hash(owner_id, token_hash)

    table = _tokens_table()
    denied = False
    owner_hash = ((await container_repo.get_by_owner_id(owner_id, consistent=True)) or {}).get(
        "bedrock_gate_token_hash"
    )
    if owner_hash:
        resp = await run_in_thread(table.get_item, Key={"token_hash": owner_hash}, ConsistentRead=True)
        denied = bool((resp.get("Item") or {}).get("denied"))
    try:
        await run_in_thread(
            table.put_item,
            Item={
                "token_hash": token_hash,
                "owner_id": owner_id,
                "agent_id": agent_id,
                "denied": denied,
                "created_at": utc_now_iso(),
            },
            ConditionExpression="attribute_not_exists(token_hash)",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    return token


async def _swap_hash_pointer(
    owner_id: str,
    token_hash: str,
    *,
    superseded: list[str],
    expected_prior_hash: str | None,
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
    ``mint_gate_token`` swap — so two concurrent provisions need 1 + 1 = 2 and
    the third is spare. Exhaustion is a LOGGED leak, not a lockout.
    """
    expected = expected_prior_hash
    outcome, conflicting = "conflict", None
    for _ in range(3):
        outcome, conflicting = await container_repo.swap_gate_token_hash(
            owner_id,
            new_hash=token_hash,
            superseded=superseded,
            expected_prior_hash=expected,
        )
        if outcome != "conflict":
            break
        if conflicting and conflicting.get("bedrock_gate_token_hash") == token_hash:
            # A concurrent run for the same generation already pointed the row here.
            outcome = "written"
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
    hashes = [
        row.get("bedrock_gate_token_hash"),
        *(row.get(_SUPERSEDED_FIELD) or []),
        *sorted(row.get("agent_gate_token_hashes") or ()),
    ]
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
    the state this module preserves: mint B, the rotation never lands, the boot
    secret still carries A, the workspace does all its inference with
    A — and the deny lands on B, a row nothing presents. Under the TTL this
    replaced, A was reaped within an hour; here it lives indefinitely, so the
    coverage has to follow it.

    The set is the same one ``_drain_superseded`` judges, so a denied token can
    never outlive its tracking: expiry deletes the row outright.

    NO-OP when the owner has no hashes at all (nothing to deny): a kill switch
    is a best-effort backstop, so the absence of a token must be silent, not an
    error. A hash
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
