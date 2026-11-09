"""Fresh-provision path for a Hermes-on-AgentCore workspace.

NOT a patch of ``agentcore_runtime.ensure_runtime``: that function is a MIGRATION tool and
hard-requires an existing EFS access point and a ``stopped`` ECS row (agentcore_runtime.py:235-239),
which a fresh workspace has never had. Patching it in place would inherit "must come from ECS" into
new-workspace provisioning (report 04, risk 5).

Two steps, in order. Neither holds a lease: every write is idempotent at its source, so two
concurrent runs for one owner converge on the same end state:

1. ``prepare_hermes_workspace`` — mints (or reuses) ``hook_token``/``gateway_token``, derives the
   gate bearer, writes the ONE JSON boot secret, and writes the two tokens onto the containers row.
   Runs BEFORE step 0 (the Paperclip seed): the starter-agent loop bakes ``hook_token`` into the
   native Hermes adapter's ``apiKey`` at seed time, so seeding before minting it would ship every
   starter agent with an empty credential.
2. ``provision_hermes_runtime`` — writes the Hermes profiles to S3, creates the AgentCore runtime,
   and writes the TERMINAL row fields. Fully idempotent-return only
   once ``status="running"``. The ARN is recorded on the row NON-terminally IMMEDIATELY after
   ``CreateAgentRuntime`` returns (M4-R35/C1) — runtime names are unique
   per account, so a create that succeeds but is never recorded (a crash, a
   CREATE_FAILED seen on the single post-create status check) would otherwise hit a name conflict
   on every retry, forever. Readiness is NOT waited on: invoking a CREATING runtime fails fast
   and retries with backoff at first use. A retry
   that finds the ARN on the row (or, failing that, by listing AgentCore runtimes for the derived
   name — covers a row write that itself never landed) skips creation and reuses it.

What is reused verbatim: the IAM/naming scaffolding (``runtime_name_for``, ``ensure_agentcore_trust``),
the owner's IAM role, ``mint_gate_token``, the boot-secret writer, and the lifecycle
constants. What is dropped: ``filesystemConfigurations`` (no EFS — profiles are S3), the OpenClaw env
builder, the SFN watchdog and the long ``wait_for_running`` ECS-boot poll (``CreateAgentRuntime`` is
one synchronous call; readiness is discovered at first invoke, not waited on).

Row writes are plain scoped ``container_repo.update_fields`` calls. The one conditional write is
the CREATE_FAILED ARN clear (``expected_field=``): it must not wipe a newer, healthy ARN a concurrent run
recorded after deleting the same dead runtime.

Boot secret (M4-R4/R5): the runtime's ``environmentVariables`` carry NO credential value. One JSON
secret, ``agentcore_runtime.boot_secret_name(owner_id)`` = ``{"hookToken","gatewayToken","bedrockBearer"}`` plus the network member key,
is written via ``agentcore_runtime.write_boot_secret`` and only its NAME travels in the env, under
``agentcore_runtime.BOOT_SECRET_ENV_VAR`` — the bridge resolves it at boot with the runtime's own
execution role. ``provision_hermes_runtime`` never re-fetches this name from anywhere: it is a pure
function of ``owner_id`` (``boot_secret_name``), so it is recomputed, not looked up — the only writer
of the secret's CONTENTS is ``prepare_hermes_workspace``, which MUST have already run for this owner
(``provision_runner.run`` always calls it first).

Rotation (M4-R16): this module never rotates an already-provisioned owner's bearer -- it is
DERIVED from the row incarnation and its ``gate_bearer_generation``, so ``prepare_hermes_workspace``
re-running for a live owner (every ``reconcile_runtime``) reproduces the bearer the runtime already
holds. When a generation bump does change it, ``write_boot_secret`` reports a boot-time change and
``reconcile_runtime`` stops the idle session so the next invoke reads the new one.

Provider switches (Task 8a): there is no switch state. ``reconcile_runtime`` rewrites the profiles
from billing and bumps ``.revision``; the bridge re-reads it every turn.

BYO key (M4-R32): the plaintext value is read from Secrets Manager at provision time via
``core.services.key_service.read_byo_key`` — the same ``lightbulb/{env}/user-keys/{user}/{provider}``
secret the ECS path references by ARN, but read back directly here (AgentCore has no task-definition
``secrets:`` mechanism to resolve it for us). Only read when ``provider_choice == "byo_key"``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import container_repo
from core.services import browser_sessions
from core.services.agentcore_runtime import (
    IDLE_TIMEOUT_S,
    MAX_LIFETIME_S,
    BOOT_SECRET_ENV_VAR,
    boot_secret_name,
    runtime_name_for,
)

logger = logging.getLogger(__name__)

#: KEEP-LINE — forced-stateful core (AgentCore-era simplification design).
#: Everything else in the provisioning path is negotiable; THESE must stay
#: durable because AWS semantics demand it, and no future audit may cut them:
#:   1. The AgentCore ARN record (+ find-by-name adopt): runtime names are
#:      unique per account, so a created-but-unrecorded runtime conflicts
#:      forever. Recorded non-terminally IMMEDIATELY after Create returns.
#:   2. S3 `<owner>/hermes/<agent>/` prefixes: the bridge restores once per
#:      process and saves end-of-turn only — not reconstructible.
#:   3. Credential snapshots: boot secret, hook/gateway tokens, gate-bearer
#:      hashes, Paperclip company + starter_agent_ids (rotation/orphan rules
#:      need durable rows).
#: Explicitly NOT kept: any wait on AWS. Readiness is discovered at first
#: invoke (retry-at-use); only a TERMINAL status (CREATE_FAILED/UPDATE_FAILED)
#: is acted on, via a single status read — never a poll.
_TERMINAL_STATUSES = ("CREATE_FAILED", "UPDATE_FAILED")


class AgentCoreRuntimeCreateFailed(RuntimeError):
    """F1 (predeploy review, M6-R34): the runtime reached a TERMINAL failure status
    (``CREATE_FAILED``/``UPDATE_FAILED``) -- dead, and AWS will never accept an
    ``UpdateAgentRuntime`` against it. Surfaced by the single post-create status
    check in ``provision_hermes_runtime`` (and the roll path), which deletes the
    dead runtime and clears the row's ARN instead of leaving a retry stuck
    reusing it forever."""


def build_hermes_runtime_env(
    *,
    owner_id: str,
    api_base: str,
    bucket: str,
    boot_secret_id: str,
    browser_profile_id: str = "",
    managed_gate_url: str = "",
    managed_model_id: str = "",
    memory_id: str = "",
    max_concurrent_turns: int | None = None,
    network_workers: bool = False,
) -> dict[str, str]:
    """The bridge's environment (M4-R19). Identifiers ONLY — every credential lives in the ONE
    Secrets Manager boot secret ``boot_secret_id`` names; nothing here is a bearer, a hook token,
    or a gateway token. Mirrors the working dev-spike set minus the three plaintext values it
    hand-patched in before this boot secret existed.

    ``browser_profile_id`` adds the AgentCore Browser block, ALL SIX KEYS OR NONE. The last
    of them is the gate: Hermes advertises ``browser_*`` only when
    ``AGENT_BROWSER_EXECUTABLE_PATH`` names an existing file (a bare ``os.path.isfile``,
    never a Chromium check), and the image bakes a zero-byte sentinel at that path. Keeping
    the gate variable inside the same ``if`` is what makes "no profile" mean "no browser
    tools advertised" rather than "a browser tool that fails on every call".
    """
    env = {
        "ISOL8_OWNER_ID": owner_id,
        "ISOL8_API_URL": api_base.rstrip("/"),
        "AWS_REGION": settings.AWS_REGION,
        "S3_BUCKET": bucket,
        # Owner FIRST: the per-owner S3 grant is <bucket>/<owner>/*
        # (bedrock_aip.build_owner_litestream_s3_policy). Any other order is outside every
        # owner's grant and AccessDenies on the first restore.
        "S3_PREFIX": f"{owner_id}/hermes",
        "HERMES_HOME": "/opt/data",
        "HERMES_HEADLESS": "1",
        # I1: harmless and explicit -- already baked into the image (Dockerfile.spike:22)
        # alongside HERMES_HOME/HERMES_HEADLESS/AGENTCORE_PORT; setting it here too means the
        # runtime request itself documents the full working set rather than half of it.
        "PYTHONUNBUFFERED": "1",
        BOOT_SECRET_ENV_VAR: boot_secret_id,
    }
    # Workspace memory: present iff configured (the roll diffs env keys, so never an empty value).
    # The image enables its agentcore memory provider only when this is set; an older image
    # ignores it.
    if memory_id:
        env["AGENTCORE_MEMORY_ID"] = memory_id
    # The network sidecar verifies members' sign-in proofs against this PUBLIC key (not a
    # credential): ``<kid>:<base64url>``. Absent: the engine's baked-in keys only.
    if settings.NETWORK_PROOF_KEY:
        from core.services import network_proof

        env["LIGHTBULB_ISSUER_KEY"] = network_proof.public_key()["env"]
    # The bridge's per-microVM turn cap; absent keeps the image default (4).
    if max_concurrent_turns:
        env["LIGHTBULB_MAX_CONCURRENT_TURNS"] = str(int(max_concurrent_turns))
    # This workspace's agents take network assignments (bridge/network.py); absent keeps them off.
    if network_workers:
        env["LIGHTBULB_NETWORK_WORKERS"] = "1"
    if managed_gate_url:
        env["LIGHTBULB_MANAGED_GATE_URL"] = managed_gate_url
        env["LIGHTBULB_MANAGED_MODEL_ID"] = managed_model_id
    if browser_profile_id:
        env.update(
            {
                "AGENT_BROWSER_PROVIDER": "agentcore",
                "AGENTCORE_REGION": settings.AWS_REGION,
                # Lockstep: the SAME constant the owner's IAM grant and the backend's own
                # session calls use. Three independent literals here would fail silently
                # and split (a 403 on start, or a permanently dead panel).
                "AGENTCORE_BROWSER_ID": browser_sessions.BROWSER_ID,
                "AGENTCORE_PROFILE_ID": browser_profile_id,
                # The CLI's own default is 3600; a turn's session lives 4-15 s.
                "AGENTCORE_SESSION_TIMEOUT": "900",
                "AGENT_BROWSER_EXECUTABLE_PATH": "/opt/lightbulb/no-local-browser",
            }
        )
    return env


def build_hermes_runtime_request(
    *,
    owner_id: str,
    image_uri: str,
    role_arn: str,
    subnets: list[str],
    security_groups: list[str],
    environment: dict[str, str],
) -> dict:
    """The CreateAgentRuntime request for a fresh Hermes workspace. No
    ``filesystemConfigurations`` — a Hermes profile is a complete S3-resident ``$HERMES_HOME``,
    not an EFS mount."""
    return {
        "agentRuntimeName": runtime_name_for(owner_id),
        "agentRuntimeArtifact": {"containerConfiguration": {"containerUri": image_uri}},
        "roleArn": role_arn,
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {"securityGroups": security_groups, "subnets": subnets},
        },
        "protocolConfiguration": {"serverProtocol": "HTTP"},
        "lifecycleConfiguration": {
            "idleRuntimeSessionTimeout": IDLE_TIMEOUT_S,
            "maxLifetime": MAX_LIFETIME_S,
        },
        "environmentVariables": environment,
        "tags": {"owner_id": owner_id, "Environment": settings.ENVIRONMENT, "ManagedBy": "lightbulb-backend"},
    }


#: M6-R44: ``ensure_agentcore_trust`` updates the owner role's trust policy seconds before
#: CreateAgentRuntime, and IAM propagates trust changes with a delay, so AgentCore's role
#: validation rejects a freshly trusted role on the first call (seen on the first fresh-org dev
#: migration: "Role validation failed ... verify that the role exists and its trust policy
#: allows assumption by this service"; the retry a minute later succeeded). Bounded retry of
#: that ONE ValidationException; every other error raises at once.
_ROLE_PROPAGATION_ATTEMPTS = 12
_ROLE_PROPAGATION_INTERVAL_S = 5.0


#: How long a reprovision waits for the previous same-named runtime to finish DELETING
#: (a teardown immediately followed by a provision hits this; AgentCore names are deterministic).
_DELETE_WAIT_TIMEOUT_S = 90.0  # two waits + role retries (60 s) stay inside the Lambda's 5 min
_DELETE_WAIT_INTERVAL_S = 5.0


def _wait_runtime_gone(runtime_arn: str) -> bool:
    """Poll ``get_agent_runtime`` until the runtime no longer exists. Sync — call via
    ``run_in_thread``. True once gone; False if still present at the timeout (or if it
    turns out not to be deleting, i.e. a real conflict)."""
    client = _agentcore_control_client()
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    deadline = time.monotonic() + _DELETE_WAIT_TIMEOUT_S
    while True:
        try:
            status = client.get_agent_runtime(agentRuntimeId=runtime_id).get("status")
        except client.exceptions.ResourceNotFoundException:
            return True
        if status != "DELETING" or time.monotonic() >= deadline:
            return False
        time.sleep(_DELETE_WAIT_INTERVAL_S)


def _create_runtime(request: dict) -> str:
    """Sync — call via ``run_in_thread``. Retries only the IAM trust-propagation rejection
    (M6-R44, see the constants above). A ConflictException (a concurrent provision run already
    created this owner's uniquely-named runtime) adopts the existing ARN via the same by-name
    lookup ``provision_hermes_runtime`` uses for a crash-recovered ARN, instead of failing — the
    owner ruling: no leases, both runs must converge.

    Fix round 1 (Important #1): ``_find_existing_runtime_arn``'s ``list_agent_runtimes`` still
    lists a runtime that is DELETING (M6-R43), so the name conflict can be held by a runtime
    that is about to not exist — e.g. a peer run saw CREATE_FAILED, deleted it, and is mid-clear
    on the row. Adopting it blind would record a dead ARN as a SUCCESS. ``_runtime_image_uri``
    already raises ``_RuntimeGone`` for exactly that (DELETING or already gone); on that, the
    ORIGINAL conflict is re-raised so it classifies transient and retries once the delete
    finishes, instead of the adopt succeeding."""

    client = _agentcore_control_client()
    return _create_with_role_retries(client, request, allow_conflict_wait=True)


def _create_with_role_retries(client, request: dict, *, allow_conflict_wait: bool) -> str:
    """The bounded create loop. On a name conflict with a DELETING runtime (first pass only) it
    waits the delete out, then runs this loop once more with its own fresh role-propagation
    budget and no further conflict wait."""
    for attempt in range(1, _ROLE_PROPAGATION_ATTEMPTS + 1):
        try:
            return client.create_agent_runtime(**request)["agentRuntimeArn"]
        except client.exceptions.ConflictException as conflict:
            arn = _find_existing_runtime_arn(request["agentRuntimeName"])
            if arn is None:
                raise
            try:
                _runtime_image_uri(arn)
            except _RuntimeGone:
                # DELETING (or just gone): wait the delete out, then create again. A runtime
                # that never finishes deleting (or a second conflict) re-raises the conflict.
                if not allow_conflict_wait or not _wait_runtime_gone(arn):
                    raise conflict from None
                return _create_with_role_retries(client, request, allow_conflict_wait=False)
            return arn
        except client.exceptions.ValidationException as exc:
            if "Role validation failed" not in str(exc):
                raise
            if attempt == _ROLE_PROPAGATION_ATTEMPTS:
                # IAM propagation is transient: surface it as retryable, not a deterministic ValidationException.
                raise RuntimeError(f"role still not visible to AgentCore after {attempt} attempts: {exc}") from exc
            logger.info("hermes provision: role not yet visible to AgentCore (attempt %d) -- retrying", attempt)
            time.sleep(_ROLE_PROPAGATION_INTERVAL_S)
    # Retryable by classify_provision_error (a plain RuntimeError), never an unclassified AssertionError.
    raise RuntimeError("CreateAgentRuntime: role-propagation attempts exhausted")


def _find_existing_runtime_arn(name: str) -> str | None:
    """M4-R35: an owner's runtime name is unique per account, so a retry whose row lost the
    ARN (a crash between ``CreateAgentRuntime`` and the row write) would
    otherwise hit a name conflict on every future attempt, forever. Sync — call via
    ``run_in_thread``.

    N3/M4-R41: paginates. A single unpaginated call only ever saw page 1 of
    ``list_agent_runtimes`` (``maxResults`` <= 100) -- one runtime per owner means the fleet
    outgrows page 1 eventually, and this recovery lookup would then silently miss every runtime
    past it, forever hitting a name conflict on create instead of finding the existing one.
    """
    client = _agentcore_control_client()
    paginator = client.get_paginator("list_agent_runtimes")
    for page in paginator.paginate():
        for runtime in page.get("agentRuntimes", []):
            if runtime.get("agentRuntimeName") == name:
                return runtime.get("agentRuntimeArn")
    return None


class _RuntimeGone(Exception):
    """The runtime an ARN names no longer exists, or is being deleted (M6-R43)."""


def _runtime_image_uri(runtime_arn: str) -> str | None:
    """The image URI an existing runtime is currently configured with. Sync — call via
    ``run_in_thread``. Raises ``_RuntimeGone`` exactly as ``_runtime_description`` does."""
    return _image_of(_runtime_description(runtime_arn))


def _image_of(desc: dict) -> str | None:
    return (desc.get("agentRuntimeArtifact") or {}).get("containerConfiguration", {}).get("containerUri")


def _runtime_description(runtime_arn: str) -> dict:
    """One ``GetAgentRuntime`` -- the runtime's live spec (image, ``environmentVariables``).
    Sync — call via ``run_in_thread``.

    M6-R43: raises ``_RuntimeGone`` when the runtime is DELETING or already gone. A rollback
    (``teardown_hermes_runtime``) deletes the runtime, but a re-migration can still meet its
    ARN -- stamped on the row by an earlier attempt, or returned by the name lookup while the
    delete is in flight (``list_agent_runtimes`` lists DELETING runtimes). Neither is a runtime
    to reuse: the caller takes the create path, and a still-in-flight delete surfaces as a
    transient name conflict that retries cover.
    """
    client = _agentcore_control_client()
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    try:
        desc = client.get_agent_runtime(agentRuntimeId=runtime_id)
    except client.exceptions.ResourceNotFoundException:
        raise _RuntimeGone(runtime_arn) from None
    if desc.get("status") == "DELETING":
        raise _RuntimeGone(runtime_arn)
    return desc


def _runtime_status(runtime_arn: str) -> str:
    """One ``GetAgentRuntime`` status read. Sync — call via ``run_in_thread``.

    Optimistic-create contract: callers NEVER poll this. Readiness is
    discovered at first invoke (retry-at-use); only ``_TERMINAL_STATUSES``
    need action here.
    """
    client = _agentcore_control_client()
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    return client.get_agent_runtime(agentRuntimeId=runtime_id)["status"]


def _update_runtime_image(runtime_arn: str, request: dict) -> None:
    """Bring a reused (not freshly created) runtime onto the currently configured image --
    e.g. a broken tag fixed between retries. Sync — call via ``run_in_thread``.

    N2/M4-R40: ``UpdateAgentRuntime`` is a FULL REPLACE and requires ``roleArn`` (verified
    against the installed botocore service model: it is a REQUIRED member, alongside
    ``agentRuntimeId``/``agentRuntimeArtifact`` -- the previous call sent only the latter two and
    raised ``ParamValidationError`` on every invocation). Omitting ``networkConfiguration``/
    ``environmentVariables``/``protocolConfiguration``/``lifecycleConfiguration`` on a full replace
    would also silently drop the VPC mode, every env var, and the 4h/8h lifecycle. Reuses
    ``request`` -- the SAME dict ``build_hermes_runtime_request`` produced for create -- so update
    and create can never drift apart; strips ``agentRuntimeName``/``tags``, which the model does
    NOT accept on update (confirmed: neither is a member of ``UpdateAgentRuntime``'s input shape).
    """
    client = _agentcore_control_client()
    runtime_id = runtime_arn.rsplit("/", 1)[-1]
    update_kwargs = {k: v for k, v in request.items() if k not in ("agentRuntimeName", "tags")}
    # IMDSv2-only metadata (token PUT before any GET), pinned rather than left to AWS's default.
    # UPDATE-only: CreateAgentRuntime has no such member, and every runtime created so far already
    # reads requireMMDSV2 true (checked all 36 on 2026-09-26) -- this keeps a roll from ever
    # depending on that default. It does not stop the agent's shell doing the PUT+GET itself.
    update_kwargs["metadataConfiguration"] = {"requireMMDSV2": True}
    client.update_agent_runtime(agentRuntimeId=runtime_id, **update_kwargs)


def _agentcore_control_client():
    """The AgentCore CONTROL plane (runtime create/update/delete/describe).

    Built per call, deliberately: every test in this module's suite monkeypatches
    ``boto3.client`` globally, which a module-level cached client would outlive.
    Construction is a few hundred microseconds and none of these paths is hot.
    """
    import boto3

    return boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)


def _agentcore_data_client():
    """The AgentCore DATA plane (InvokeAgentRuntime, StopRuntimeSession) -- a different
    service name from the control client above, not an alias for it."""
    import boto3

    return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


def _delete_runtime(runtime_arn: str) -> None:
    """``DeleteAgentRuntime``. Sync — call via ``run_in_thread``. Shared by F1's terminal-runtime
    cleanup (below) and ``teardown_hermes_runtime`` (F5)."""
    _agentcore_control_client().delete_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])


def _stop_runtime_session(runtime_arn: str, session_id: str) -> None:
    """``StopRuntimeSession`` (M4-R16) — must run BEFORE the old bearer generation is revoked: a
    warm AgentCore session caches the bearer it read from the boot secret at cold-start, so
    denying/deleting the token row alone leaves an already-warm session presenting a now-dead
    credential until AWS reaps it on its own. Sync — call via ``run_in_thread``."""
    _agentcore_data_client().stop_runtime_session(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id)


async def stop_runtime_session(owner_id: str, runtime_arn: str, task_id: str | None = None) -> bool:
    """THE way to stop an owner's session -- roll, ``--respec``, reconcile restart, the debug
    PATCH, teardown, and any recycle. Never call ``_stop_runtime_session`` directly: this also
    clears the backend's ``__session__#<owner>`` warm estimate, and a stop the backend never saw
    leaves that estimate reading "warm" over a dead session -- the one error that lets
    concurrent cold invokes fan a workspace across several microVMs (``runtime_transport``).

    Returns True when the session is confirmed gone (stopped, or already not found); never raises.

    One call, no loop: on dev (2026-09-25) a forced three-microVM split answered the first stop
    by SIGTERMing ONE microVM and every later stop with ``ResourceNotFoundException`` while the
    other two stayed alive, so repeating the stop cleans up nothing. Those leftovers age out at
    their own idle/lifetime limit; the single-flight wake is what keeps them from existing.

    ``task_id`` stops that task's own session (super-agent per-task VMs) and clears only its
    estimate row; the workspace session stays warm.
    """
    from botocore.exceptions import ClientError

    from core.repositories import container_repo
    from core.services.runtime_transport import session_id_for

    try:
        before_stop = await run_in_thread(
            container_repo.get_session_sync, owner_id, **({"task_id": task_id} if task_id is not None else {})
        )
    except Exception:  # noqa: BLE001 -- no stop without the generation needed to clear safely
        logger.warning("session snapshot failed before stop owner=%s", owner_id, exc_info=True)
        return False
    try:
        await run_in_thread(_stop_runtime_session, runtime_arn, session_id_for(owner_id, task_id))
        stopped = True
    except ClientError as exc:
        # No session to stop is as good as a stop: the next one cold-starts.
        stopped = exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException"
        if not stopped:
            logger.warning("StopRuntimeSession failed for owner=%s: %s", owner_id, exc)
    except Exception as exc:  # noqa: BLE001 -- unconfirmed; the caller decides what that means
        stopped = False
        logger.warning("StopRuntimeSession failed for owner=%s: %s", owner_id, type(exc).__name__)
    try:
        # Cleared even on an unconfirmed stop: "cold" is the safe error (one serialized health).
        await container_repo.clear_session(
            owner_id,
            expected_arn=runtime_arn,
            expected_session=before_stop or {},
            **({"task_id": task_id} if task_id is not None else {}),
        )
    except Exception:  # noqa: BLE001
        logger.warning("session estimate clear failed for owner=%s", owner_id, exc_info=True)
    return stopped


def _delete_boot_secret(owner_id: str) -> None:
    """Force-delete the owner's Hermes boot secret (no recovery window — a torn-down owner's
    credentials are dead the moment the runtime they were minted for is gone). Sync — call via
    ``run_in_thread``."""
    import boto3

    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        sm.delete_secret(SecretId=boot_secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        pass


def _delete_profile_prefix_objects(owner_id: str) -> None:
    """Delete every S3 object under ``<owner>/hermes/`` -- the whole Hermes home, every agent's
    profile plus the root ``auth.json``. Only called by ``teardown_hermes_runtime`` when
    ``delete_profiles=True`` (an account wipe, never a rollback). Sync — call via
    ``run_in_thread``."""
    from core.services.hermes_profiles import root_home_prefix

    _delete_prefixes(
        [
            root_home_prefix(owner_id),
            f"marketplace-installs/{owner_id}/",
            f"browser-state/{owner_id}/",
            f"{owner_id}/browser-extensions/",
        ]
    )


def _delete_prefixes(prefixes: list[str]) -> None:
    """Delete every owner-state object under each prefix. Paginated (S3's own 1000-key batch
    cap); raises on a partial delete. A no-op without a bucket. Sync — call via
    ``run_in_thread``."""
    if not settings.OWNER_STATE_BUCKET:
        return
    import boto3

    client = boto3.client("s3")
    paginator = client.get_paginator("list_objects_v2")
    for prefix in prefixes:
        for page in paginator.paginate(Bucket=settings.OWNER_STATE_BUCKET, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                result = client.delete_objects(Bucket=settings.OWNER_STATE_BUCKET, Delete={"Objects": keys})
                if (result or {}).get("Errors"):
                    raise RuntimeError("Workspace state deletion was incomplete")


async def teardown_hermes_runtime(owner_id: str, *, container_row: dict, delete_profiles: bool) -> None:
    """The AgentCore-side teardown (F5, predeploy review, M6-R38) -- the ONE place that undoes
    what ``prepare_hermes_workspace``/``provision_hermes_runtime`` created, so every caller that
    needs to tear a workspace down shares it instead of re-deriving the AWS call sequence.

    Steps, each best-effort -- an already-gone runtime/secret/hash/prefix must never stop the
    caller's own teardown (an account wipe deleting the containers row, a reprovision clearing
    the runtime first):

      1. ``StopRuntimeSession`` (M4-R16) -- a warm session caches the bearer it read at
         cold-start; skipping this can leave it presenting a dead credential until AWS reaps it.
      2. ``DeleteAgentRuntime``.
      3. Revoke the row's own ``bedrock_gate_token_hash`` (the live bearer generation).
      4. Delete the ONE JSON boot secret, and the workspace's ``<owner>/network/`` state
         (whatever ``delete_profiles`` says: the member key dies with the secret, so the state
         that key signed is dead too).
      5. (``delete_profiles`` only) delete every object under ``<owner>/hermes/`` in S3.

    ``delete_profiles=False`` is the default and what a reprovision wants -- S3 profiles are
    cheap and the next provision overwrites them. ``delete_profiles=True`` is for a caller after
    which NOTHING will ever provision this owner's workspace again (an account wipe, the debug
    user-data delete) -- otherwise the whole
    ``<owner>/hermes/`` prefix (every profile, the root ``auth.json``) orphans indefinitely in
    S3 with no row left to even name it.

    ``container_row`` supplies ``agentcore_runtime_arn`` (the runtime to stop+delete) and
    ``bedrock_gate_token_hash`` (the bearer generation to revoke) -- the caller's OWN
    already-read row, never re-read here (a wipe's caller already holds the authoritative
    ``ALL_OLD`` image; a rollback's caller already read it strongly consistent).
    """
    from core.services import browser_control

    try:
        await browser_control.teardown(owner_id, container_row, delete_state=delete_profiles)
    except Exception:
        if not delete_profiles:
            raise
        logger.warning("Browser teardown failed for owner=%s", owner_id)

    from core.services.runtime_transport import live_arn

    arn = await run_in_thread(live_arn, container_row)
    if arn:
        await stop_runtime_session(owner_id, arn)  # best-effort; never raises
    # Deletes only the row's OWN runtime (never the shared one, which no row names).
    await _delete_runtime_and_bearer(owner_id, container_row)

    try:
        await run_in_thread(_delete_boot_secret, owner_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.warning("hermes teardown: could not delete boot secret for owner=%s", owner_id, exc_info=True)
    from core.services.network_member import network_prefix

    try:
        await run_in_thread(_delete_prefixes, [network_prefix(owner_id)])
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.warning("hermes teardown: could not delete network state for owner=%s", owner_id, exc_info=True)

    if delete_profiles:
        try:
            await run_in_thread(_delete_profile_prefix_objects, owner_id)
        except Exception:  # noqa: BLE001 — best-effort cleanup
            logger.warning("hermes teardown: could not delete S3 profiles for owner=%s", owner_id, exc_info=True)


async def _delete_runtime_and_bearer(owner_id: str, container_row: dict) -> None:
    """``DeleteAgentRuntime`` on the row's runtime and revoke the row's gate token: the two names
    that belong to this runtime alone, not to the owner. Best-effort; never raises."""
    from core.billing.bedrock_gate_tokens import delete_gate_token_rows

    arn = container_row.get("agentcore_runtime_arn")
    if arn:
        try:
            await run_in_thread(_delete_runtime, arn)
        except Exception:  # noqa: BLE001 — best-effort: the runtime may already be gone
            logger.warning(
                "hermes teardown: DeleteAgentRuntime failed for owner=%s arn=%s", owner_id, arn, exc_info=True
            )

    current_hash = container_row.get("bedrock_gate_token_hash")
    if current_hash:
        try:
            await delete_gate_token_rows([current_hash])
        except Exception:  # noqa: BLE001 — best-effort: a stranded row is a known, accepted risk elsewhere too
            logger.warning("hermes teardown: could not revoke gate token for owner=%s", owner_id, exc_info=True)


async def _superseded(owner_id: str, container: dict) -> bool:
    """A new workspace was provisioned under this owner id after the wipe deleted the row: its
    boot secret carries the old member key over, so every owner-keyed name is now ITS. The row id
    tells the incarnations apart on the shared runtime, where neither row carries an ARN."""
    row = await container_repo.get_by_owner_id(owner_id)
    return bool(row) and any(row.get(k) != container.get(k) for k in ("id", "agentcore_runtime_arn"))


_runtime_teardowns: set[asyncio.Task] = set()
FORGET_RETRY_S = 20
FORGET_WINDOW_S = 180
# The durable copy of a scheduled teardown, for a backend task that stops inside the window.
# Due after the in-process window plus a try's 90 s ingress floor, so the sweep does not race it.
TEARDOWN_PK = "TEARDOWN#"
TEARDOWN_DUE_S = 200
TEARDOWN_TTL_S = 30 * 86400
# ``status`` routes an ARN-less (shared-runtime) row to the pool: ``runtime_transport.runtime_arn``.
_TEARDOWN_ROW_KEYS = (
    "owner_id",
    "id",
    "agentcore_runtime_arn",
    "agentcore_shared_runtime_arn",
    "status",
    "gateway_token",
    "bedrock_gate_token_hash",
)


def _teardown_key(owner_id: str, container: dict) -> dict:
    return {"pk": TEARDOWN_PK, "sk": f"{owner_id}#{container.get('agentcore_runtime_arn') or ''}"}


async def _schedule_teardown(owner_id: str, container: dict, author: str) -> None:
    """Write the ``TEARDOWN#`` row, then start the in-process task. A failed write is logged and
    the task still runs: only the restart safety net is lost."""
    from core.repositories import messaging_repo

    # What the forget's invoke and the teardown read: the runtime, its bearer and gate token.
    row = {k: container[k] for k in _TEARDOWN_ROW_KEYS if container.get(k)}
    now = int(time.time())
    item = {
        **_teardown_key(owner_id, row),
        "ownerId": owner_id,
        "row": row,
        "author": author,
        "dueAt": now + TEARDOWN_DUE_S,
        "expiresAt": now + TEARDOWN_TTL_S,
    }
    try:
        await run_in_thread(messaging_repo._table().put_item, Item=item)
    except Exception as exc:  # noqa: BLE001
        logger.error("owner wipe: teardown row not written owner=%s error=%s", owner_id, type(exc).__name__)
    task = asyncio.create_task(_forget_then_teardown(owner_id, row, author))
    _runtime_teardowns.add(task)
    task.add_done_callback(_runtime_teardowns.discard)


async def run_due_teardowns() -> int:
    """The sweep's half: a ``TEARDOWN#`` row past due means its backend task stopped. One forget
    try, then the teardown (same guard), then the row goes. Returns how many ran."""
    from core.repositories import messaging_repo

    ran = 0
    for item in await messaging_repo._query(TEARDOWN_PK):
        now = time.time()
        if int(item.get("dueAt") or 0) > now or int(item.get("expiresAt") or 0) <= now:
            continue
        await _forget_then_teardown(str(item["ownerId"]), dict(item["row"]), str(item["author"]), window_s=0)
        ran += 1
    return ran


async def _forget_then_teardown(owner_id: str, container: dict, author: str, *, window_s: float | None = None) -> None:
    """An owner wipe whose cloud member was parked: retry ``memory.forget_author`` against the
    captured row (the containers row is already deleted; an invoke cold-starts the runtime), then
    tear the runtime down whatever happened. Never raises. If the owner id was provisioned again
    meanwhile (``_superseded``), the late forget would tombstone the new workspace's entries and
    the owner-keyed deletes would take its boot secret, network state and profiles: then only the
    captured runtime and its gate token go. Its ``TEARDOWN#`` row is deleted once it is done; a
    teardown that raises keeps the row for the sweep."""
    from core.repositories import messaging_repo
    from core.services import container_rpc

    retry_s = FORGET_RETRY_S
    deadline = time.monotonic() + (FORGET_WINDOW_S if window_s is None else window_s)
    forgot = superseded = False
    while not forgot:
        if superseded := await _superseded(owner_id, container):
            break
        try:
            await container_rpc.call(owner_id, "memory.forget_author", {"author": author}, target=container)
            forgot = True
            try:  # the owed row the forget answered (its sweep never runs once the row is gone)
                key = {"pk": f"MEMWIPE#{owner_id}", "sk": f"A#{author}"}
                await run_in_thread(messaging_repo._table().delete_item, Key=key)
            except Exception as exc:  # noqa: BLE001 -- it expires on its own
                logger.warning("owner wipe: owed row not deleted owner=%s error=%s", owner_id, type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 -- still parked or failing: try again inside the window
            logger.info("owner wipe: forget_author retry owner=%s error=%s", owner_id, type(exc).__name__)
            if time.monotonic() + retry_s > deadline:
                break
            await asyncio.sleep(retry_s)
    try:
        if superseded or await _superseded(owner_id, container):
            await _delete_runtime_and_bearer(owner_id, container)
            logger.warning("owner wipe: owner provisioned again; only the old runtime deleted owner=%s", owner_id)
        else:
            await teardown_hermes_runtime(owner_id, container_row=container, delete_profiles=True)
            logger.info("owner wipe: runtime torn down owner=%s forgot=%s", owner_id, forgot)
        await run_in_thread(messaging_repo._table().delete_item, Key=_teardown_key(owner_id, container))
    except Exception:  # noqa: BLE001 -- nothing awaits this task; a kept row is the sweep's retry
        logger.exception("owner wipe: scheduled runtime teardown failed owner=%s", owner_id)


async def delete_workspace_runtime(
    owner_id: str, *, delete_profiles: bool = False, report: dict | None = None
) -> dict | None:
    """THE TEARDOWN SEAM. Tear a workspace's runtime down and delete its containers row.

    Every teardown in the system reaches AWS through this function -- entitlement
    cancellation, the admin account wipe, ``DELETE /debug/provision``,
    ``DELETE /debug/user-data`` -- which is what makes the Bedrock reap below structural:
    no caller can forget it, and none needs its own copy.

    Steps:
      1. ``teardown_hermes_runtime`` -- stop the session, delete the runtime, revoke the
         gate-token hash, delete the boot secret; delete every object under
         ``<owner>/hermes/`` only when ``delete_profiles=True``.
      2. Delete the containers row. UNCONDITIONAL, and after the teardown: the read above
         is eventually consistent, so gating on it would leave a racing provision's row
         behind along with everything only it names. The DELETE's ``ALL_OLD`` image is the
         authoritative answer to "what did this row say when it died".
      3. Reap the owner's Bedrock footprint (gate-token rows, AIPs, IAM role) off that
         image -- the row was the only handle on it. Raises on a partial reap rather than
         reporting success: the runtime is already gone, so a raise honestly means
         "teardown incomplete, retry me", and every step is idempotent.
      4. Sweep the owner's ``channel_links`` rows (non-fatal).
      5. (``delete_profiles`` only) wipe the owner's workspace memory. Raises on failure. Its
         network half runs first, before step 1: the cloud member forgets the entries it signed
         (``workspace_memory.forget_workspace_authored``), which needs the live runtime. A parked
         member owes it: step 1 then runs in a background task (``_forget_then_teardown``) after
         the forget, or after ``FORGET_WINDOW_S`` regardless. ``report["runtime"]`` (when a dict
         is passed) says ``"done"`` or ``"scheduled"``.

    ``delete_profiles`` (M6-R42) defaults to **False** and must stay that way: this
    function is shared by NON-terminal callers -- ``entitlement_teardown`` (a lapsed or
    past-due subscription) and ``routers/debug.py``'s ``DELETE /debug/provision`` (a
    routine reprovision) -- and passing ``True`` from those destroyed a migrated owner's
    memory / skills / SOUL.md / auth.json. Exactly THREE terminal callers pass ``True``:
    ``account_wipe`` (user-keyed), ``workspace_wipe`` (org-keyed), and
    ``routers/debug.py``'s ``DELETE /debug/user-data`` (the e2e
    empty-slate reset; debug is 403 in prod). Do not re-broaden it.

    Returns the containers row as it was at the instant it was deleted, or None when
    there was none.
    """
    container = await container_repo.get_by_owner_id(owner_id)
    if container:
        owed = None
        if delete_profiles:
            from core.services import workspace_memory

            # While the runtime and its member key still exist; never raises.
            owed = await workspace_memory.forget_workspace_authored(owner_id)
        if owed:
            # Parked or failing: the runtime outlives this request until the member forgets. The
            # browser goes now, while its state row (deleted below) still names its session.
            from core.services import browser_control

            try:
                await browser_control.teardown(owner_id, container, delete_state=True)
            except Exception:  # noqa: BLE001 -- like teardown_hermes_runtime's delete_profiles case
                logger.warning("Browser teardown failed for owner=%s", owner_id)
            await _schedule_teardown(owner_id, container, owed)
        else:
            await teardown_hermes_runtime(owner_id, container_row=container, delete_profiles=delete_profiles)
        if report is not None:
            report["runtime"] = "scheduled" if owed else "done"

    deleted = await container_repo.delete(owner_id)

    from core.billing.bedrock_aip import reap_owner_bedrock_resources

    if delete_profiles:
        await container_repo.delete_browser_state(owner_id)
        if not container:
            await run_in_thread(_delete_profile_prefix_objects, owner_id)
    reaped = await reap_owner_bedrock_resources(owner_id, deleted, delete_browser_profile=delete_profiles)
    logger.info("Bedrock reap for owner %s: %s", owner_id, reaped)

    try:
        from core.repositories import channel_link_repo

        link_count = await channel_link_repo.sweep_by_owner(owner_id)
        if link_count:
            logger.info("Swept %d channel_link rows for deleted workspace (owner=%s)", link_count, owner_id)
    except Exception:  # noqa: BLE001 — the runtime is already gone; orphan link rows are cheap
        logger.exception("Failed to sweep channel_link rows for owner %s after teardown", owner_id)

    if delete_profiles:
        # Workspace memory is the owner's data like the profiles are. LAST and not swallowed: its
        # records never expire, so a failure must say "teardown incomplete, retry me" -- and a
        # retry arrives row-less, which is why this sits outside the ``if container`` branch
        # (and after the reap, which needs this call's ``deleted`` image).
        from core.services import workspace_memory

        await workspace_memory.delete_owner_memory(owner_id)

    return deleted


async def prepare_hermes_workspace(owner_id: str) -> bool:
    """Settle stable owner tokens, gate bearer, provider credentials and network membership.

    The canonical pool receives boot values on each invoke. Preparation never creates an
    owner runtime role or writes a legacy boot secret. Existing credentials and network
    seeds remain intact. Concurrent calls converge on ensure_owner_tokens' returned row id
    and generation, so neither retries nor reconciliation rotate an active identity.
    """
    if not settings.HERMES_SHARED_RUNTIME_ARN:
        raise RuntimeError("HERMES_SHARED_RUNTIME_ARN is not configured for this environment")

    import secrets as _secrets

    from core.billing.bedrock_gate_tokens import mint_gate_token
    from core.services.container_provision import resolve_provider_choice

    hook_candidate = _secrets.token_urlsafe(32)
    gateway_candidate = _secrets.token_urlsafe(32)
    row = await container_repo.ensure_owner_tokens(owner_id, hook_token=hook_candidate, gateway_token=gateway_candidate)
    hook_token, gateway_token = row["hook_token"], row["gateway_token"]
    hook_token_reused = hook_token != hook_candidate
    gateway_token_reused = gateway_token != gateway_candidate

    # On the shared runtime no workspace has an execution role or a boot secret of its own: the
    # backend derives the boot values and sends them on every invoke (runtime_transport._boot_header).
    await mint_gate_token(owner_id, row_id=row["id"], generation=int(row.get("gate_bearer_generation") or 0))
    provider_choice, _ = await resolve_provider_choice(owner_id)
    if provider_choice == "openrouter_managed":
        from core.services.openrouter_keys import ensure_owner_key_for_settings

        await ensure_owner_key_for_settings(owner_id)
    elif provider_choice == "chatgpt_oauth":
        await _ensure_codex_auth_staged(owner_id)
    # ponytail: on the shared runtime a bearer rotation (a manual generation bump) reaches a warm
    # session at its next cold start, not by a restart here; nothing in code bumps it.
    boot_changed = False
    await _ensure_network_member(owner_id)

    # m2: the one step that mints three credentials and writes a secret was otherwise invisible
    # in the provision log. Never logs a value -- only which tokens were reused vs freshly minted.
    logger.info(
        "hermes prepare owner=%s hook_token=%s gateway_token=%s bedrock_bearer=derived boot_changed=%s",
        owner_id,
        "reused" if hook_token_reused else "minted",
        "reused" if gateway_token_reused else "minted",
        boot_changed,
    )
    return boot_changed


async def _ensure_network_member(owner_id: str) -> None:
    """Write ``sessions.json``, so every workspace is a member of the ``lightbulb`` network from
    provision on, and rewrite it when it exists so each provision, reconcile and roll carries the
    current ``members`` (the people the workspace's agents answer; a file written before that
    field existed would otherwise leave the gate off and hold every member's message).
    Best-effort: a network hiccup never fails a provision. On a fresh provision the roster is
    still empty here; ``write_profiles`` republishes it once the roster is seeded."""
    if not settings.OWNER_STATE_BUCKET:
        return
    from core.services import network_member

    try:
        if await network_member.sessions_exist(owner_id):
            await network_member.publish_sessions(owner_id)
        else:
            await network_member.write_sessions(owner_id)
    except Exception:  # noqa: BLE001
        logger.warning("network sessions write failed for owner=%s", owner_id, exc_info=True)


async def _ensure_codex_auth_staged(owner_id: str) -> None:
    """Stage the connector's ChatGPT grant into root ``auth.json`` ONLY when none is there: an
    existing grant may carry a refresh token the runtime has since rotated, and overwriting it
    with the stored one would revoke the whole token family (``container_auth``)."""
    from core.repositories import billing_repo
    from core.services.container_auth import push_oauth_credential_to_container

    connector = ((await billing_repo.get_by_owner_id(owner_id, consistent_read=True)) or {}).get(
        "oauth_connector_user_id"
    )
    if connector:
        await push_oauth_credential_to_container(owner_id=owner_id, connector_id=connector, only_if_absent=True)


def build_runtime_request_for_owner(
    owner_id: str,
    role_arn: str,
    *,
    provider_choice: str,
    browser_profile_id: str = "",
    max_concurrent_turns: int | None = None,
    network_workers: bool = False,
) -> dict:
    """The CreateAgentRuntime/UpdateAgentRuntime spec for ``owner_id`` on the CURRENTLY configured
    image (N2/M4-R40's one spec-building path, lifted out of ``provision_hermes_runtime`` so
    ``roll_runtime_image`` reuses it verbatim).

    ``browser_profile_id`` is the workspace's AgentCore Browser profile (``""`` = no browser);
    both callers resolve it with ``browser_sessions.ensure_browser_profile`` just before
    building the request, so a roll is also the door through which an existing runtime gets
    its first profile.

    The managed OpenRouter vars ride along whenever managed inference is CONFIGURED, whatever
    ``provider_choice`` is: the env block is a boot-time input, so carrying it for every
    provider is what makes a switch into or out of ``openrouter_managed`` a pure profile
    rewrite instead of a runtime re-spec plus session restart. ``provider_choice`` is only the
    guard below."""
    from core.billing.openrouter_gate_url import gate_url as openrouter_gate_url

    managed_model_id = settings.OPENROUTER_MANAGED_MODEL_ID
    if provider_choice == "openrouter_managed" and not managed_model_id:
        raise RuntimeError("managed OpenRouter model is not configured")

    return build_hermes_runtime_request(
        owner_id=owner_id,
        image_uri=settings.AGENTCORE_HERMES_IMAGE,
        role_arn=role_arn,
        subnets=[s.strip() for s in settings.ECS_SUBNETS.split(",") if s.strip()],
        security_groups=[settings.ECS_SECURITY_GROUP_ID] if settings.ECS_SECURITY_GROUP_ID else [],
        environment=build_hermes_runtime_env(
            owner_id=owner_id,
            api_base=settings.PUBLIC_API_BASE,
            bucket=settings.OWNER_STATE_BUCKET,
            boot_secret_id=boot_secret_name(owner_id),
            browser_profile_id=browser_profile_id,
            managed_gate_url=openrouter_gate_url() if managed_model_id else "",
            managed_model_id=managed_model_id,
            memory_id=settings.AGENTCORE_MEMORY_ID,
            max_concurrent_turns=max_concurrent_turns,
            network_workers=network_workers,
        ),
    )


#: Hazard 2 (local-exec persistence report): how long a roll waits for a running turn to finish
#: before giving up and leaving the warm session alone. The bridge uploads a turn's filesystem
#: writes to S3 only at the END of the turn (bridge/sync.save_profile), so stopping the session
#: mid-turn loses everything that turn wrote -- an in-progress clone, an uncommitted ``git
#: add``, a test run's output. Now that the agent's shell runs inside the microVM (M6 #2749),
#: that is real work, not a scratch dir.
#: Real wall-clock ceiling per busy owner is ~10.5 min, not 10: an AgentCore probe can take the
#: full ``runtime_transport.AGENTCORE_INGRESS_TIMEOUT_S`` (90 s) that ``container_rpc.call``
#: floors every caller to, so one iteration is up to ~105 s. ``roll_hermes_runtimes`` is a
#: sequential loop, so that is per owner, serially.
ROLL_IDLE_WAIT_S = 600.0
ROLL_IDLE_POLL_S = 15.0


#: A bridge whose ``uptimeS`` exceeds the time since the roll's ``UpdateAgentRuntime`` by more than this
#: started before the update, so it is on the OLD spec (rounding and clock skew are the slack). A warm-pool
#: microVM of the new version can only have booted after the update.
ROLL_UPTIME_SLACK_S = 30.0


async def _wait_for_idle_session(owner_id: str) -> bool:
    """Poll the bridge's ``health`` RPC with ``drain`` until no turn is in flight, up to
    ``ROLL_IDLE_WAIT_S``. False means still busy at the deadline."""
    return await _idle_payload(owner_id, time.monotonic() + ROLL_IDLE_WAIT_S) is not None


async def _idle_payload(owner_id: str, deadline: float) -> dict | None:
    """``_wait_for_idle_session`` to an absolute ``deadline``, returning the idle bridge's health payload
    (``{}`` for "no reachable runtime") or None when still busy at the deadline.

    ``drain`` makes an idle bridge take its refusal hold (``turn.hold_turns``, 120 s TTL)
    atomically with answering ``busy: 0``, so no turn can start between this answer and the
    stop. The caller must stop IMMEDIATELY on True: a turn refused during the hold is retried,
    and the run sweeper's "never started" limit is also 120 s. A busy bridge takes no hold.

    FAILS CLOSED: nothing short of a clean ``busy: 0`` — or a positive "there is no runtime
    here" — releases the stop. A CPU-starved microVM answers ``/health`` slowly precisely
    BECAUSE a turn is hammering it (the 2026-09-14 founder-container freeze shape), so reading
    a timeout as "idle" is how this guard would authorise the exact mid-turn stop it exists to
    prevent.

    **Exception TYPE cannot make that call, so the chained cause does.**
    ``runtime_transport._request_agentcore`` deliberately normalises EVERY AgentCore failure to
    an httpx shape (a botocore ``ReadTimeoutError`` and the outer ``wait_for``'s
    ``asyncio.TimeoutError`` both become ``httpx.ReadTimeout``; ``ClientError``/``BotoCoreError``
    become ``httpx.ConnectError``), and ``container_rpc.call`` folds ``httpx.HTTPError`` into
    the SAME ``ApiProblem`` it raises for a genuinely absent runtime. A starved microVM
    therefore never reaches us as ``asyncio.TimeoutError`` — it is an ``ApiProblem``. What
    separates the two is that ``container_rpc``'s transport fold is its ONLY chaining raise
    (``raise _unavailable() from exc``); every no-runtime raise leaves ``__cause__`` as None.

    ``httpx.ConnectError`` (a throttle, or a genuine ResourceNotFound) is left on the idle side
    by ruling — the timeout is the one that must not be.

    The probe is an ``InvokeAgentRuntime`` and there is no way to suppress the wake, so on a
    parked workspace it starts a session -- on the NEW image, since the update has already
    landed, and the stop tears it back down."""
    import httpx

    from core.services import container_rpc
    from core.services.messaging import ApiProblem

    while True:
        try:
            payload = await container_rpc.call(owner_id, "health", {"drain": True})
        except ApiProblem as exc:
            if not isinstance(exc.__cause__, httpx.TimeoutException):
                return {}  # no reachable runtime: there is no warm session to protect
            payload = {"busy": 1}  # it did not answer in ~95 s, which is not proof it is idle
        except Exception:  # noqa: BLE001 -- a 504 or an ok:false is not proof of idle either
            payload = {"busy": 1}
        if not (payload or {}).get("busy"):
            return payload or {}
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(ROLL_IDLE_POLL_S)


#: Env keys ``build_hermes_runtime_env`` sets only sometimes. With the desired keys they are
#: the whole set this backend OWNS on a runtime: ``roll_runtime_image`` compares only these, so
#: a key AWS adds to the returned spec can never read as drift.
_OPTIONAL_ENV_KEYS = frozenset(
    {
        "AGENTCORE_MEMORY_ID",
        "LIGHTBULB_MAX_CONCURRENT_TURNS",
        "LIGHTBULB_NETWORK_WORKERS",
        "LIGHTBULB_MANAGED_GATE_URL",
        "LIGHTBULB_MANAGED_MODEL_ID",
        "AGENT_BROWSER_PROVIDER",
        "AGENTCORE_REGION",
        "AGENTCORE_BROWSER_ID",
        "AGENTCORE_PROFILE_ID",
        "AGENTCORE_SESSION_TIMEOUT",
        "AGENT_BROWSER_EXECUTABLE_PATH",
        # Retired with Bedrock: never set any more, but still OWNED, so a runtime that carries
        # them reads as drift and the next roll re-specs it clean.
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "AWS_BEDROCK_FORCE_HTTP1",
        "ANTHROPIC_MODEL",
    }
)


def _env_drifted(actual: dict | None, desired: dict | None) -> bool:
    actual, desired = actual or {}, desired or {}
    return any(actual.get(k) != desired.get(k) for k in set(desired) | _OPTIONAL_ENV_KEYS)


async def _browser_profile_id(owner_id: str, row: dict) -> str:
    """``ensure_browser_profile`` answers "" on ANY error. Falling back to the id the row
    already records keeps a transient browser failure from reading as "remove the browser
    block" -- a re-spec plus a session restart that the next reconcile would flip back."""
    return await browser_sessions.ensure_browser_profile(owner_id) or row.get("browser_profile_id") or ""


async def _session_warm(owner_id: str, arn: str) -> bool:
    """The backend's warm estimate for this runtime's workspace session (``runtime_transport``)."""
    row = await run_in_thread(container_repo.get_session_sync, owner_id)
    return int(row.get("warm_until") or 0) > int(time.time()) and row.get("arn") == arn


async def _rewake(owner_id: str) -> float | None:
    """Start the rolled session again, on the new spec, for a workspace that was in use: otherwise the next message
    pays the 40-55 s cold start (dev walk 16: the first answer after a roll took 54 s, 39 s of it the wake). A parked
    one stays parked. Best effort: a failed wake costs only that cold start. Returns the answering bridge's
    ``uptimeS`` (None when it did not answer), which tells the roll whether the stop really replaced it."""
    from core.services import container_rpc
    from core.services.runtime_transport import AGENTCORE_INGRESS_TIMEOUT_S

    try:
        payload = await container_rpc.call(owner_id, "health", timeout_s=AGENTCORE_INGRESS_TIMEOUT_S)
    except Exception:  # noqa: BLE001
        logger.warning("roll_runtime_image: owner=%s rolled session not re-woken", owner_id, exc_info=True)
        return None
    uptime = (payload or {}).get("uptimeS")
    return float(uptime) if isinstance(uptime, (int, float)) else None


async def _stop_when_idle(owner_id: str, arn: str, *, was_warm: bool, updated_at: float | None, write: bool) -> bool:
    """Stop the owner's session at its first idle moment, until it is really replaced or ``ROLL_IDLE_WAIT_S`` runs
    out (False: the caller reports the owner busy).

    Replaced means: the stop was CONFIRMED (an unconfirmed one is retried at the next idle probe, never reported as
    done), and -- when the spec moved (``updated_at``, the monotonic time of ``UpdateAgentRuntime``) -- no bridge
    older than the update answers afterwards. Each ``StopRuntimeSession`` takes ONE microVM of a split session, and
    prod 2026-11-08 had two: the stop took 45106d09, while 395f1e3e (both booted 13:30:24 on h-2026.11.8-d99e0f8)
    answered the rewake and served four phone-caller runs on the old image, without #5105's caller limits, until it
    idled out at 14:13:47. So a warm session -- or one whose drain probe answered from an old bridge -- is woken
    after the stop, and an old ``uptimeS`` sends the roll back to drain and stop again.

    A CONFIRMED stop (or AgentCore reporting no session) is the one point at which superseded gate bearers are
    expired. ``write`` rewrites the profiles once, after the first confirmed stop (a managed-model flip: the model
    lives in the env AND each profile)."""
    from core.billing.bedrock_gate_tokens import expire_superseded_gate_tokens

    def _old(uptime) -> bool:
        return (
            updated_at is not None
            and isinstance(uptime, (int, float))
            and uptime > time.monotonic() - updated_at + ROLL_UPTIME_SLACK_S
        )

    from core.services.runtime_transport import live_arn

    async def target() -> str:
        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        return await run_in_thread(live_arn, {**row, "owner_id": owner_id}) or arn

    deadline = time.monotonic() + ROLL_IDLE_WAIT_S
    while True:
        current_arn = await target()
        payload = await _idle_payload(owner_id, deadline)
        if payload is None:
            return False
        if current_arn != await target():
            if time.monotonic() >= deadline:
                return False
            continue  # the idle probe and stop must address the same live runtime
        if await stop_runtime_session(owner_id, current_arn):
            await expire_superseded_gate_tokens(owner_id)  # never raises
            if write:
                # config.yaml is backend-owned (never saved back by the bridge), so nothing reverts this.
                # Either order leaves a gap where one turn sees a new/old env-profile pair and is refused
                # (sync.py); after the stop, that gap is a cold start that has no old env to fall back on.
                await write_profiles(owner_id)
                write = False
            if not (was_warm or _old(payload.get("uptimeS"))):
                return True
            uptime = await _rewake(owner_id)
            if not _old(uptime):
                return True
            logger.warning(
                "roll_runtime_image: owner=%s a microVM up %.0fs (older than the update) still answers after the "
                "stop -- a split session; draining and stopping it too",
                owner_id,
                uptime,
            )
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(ROLL_IDLE_POLL_S)


async def roll_runtime_image(
    owner_id: str, *, dry_run: bool = False, restart_session: bool = False, respec: bool = False
) -> str:
    """Bring an AgentCore owner's runtime onto its DESIRED spec -- the currently configured
    image AND the env block ``build_runtime_request_for_owner`` derives from billing -- then
    stop the idle warm session so the NEXT invoke boots it. After a ``hermes-version.json`` tag
    flip (M6 Task 3 flip tooling) the difference is the image; it can equally be the env.

    ``UpdateAgentRuntime`` alone does NOT move a live session -- the explicit ``StopRuntimeSession``
    is what does, immediately and regardless of what is running inside it. So the stop waits for
    the bridge to report idle first (Hazard 2, ``_wait_for_idle_session``): a roll landing
    mid-turn destroys every filesystem write that turn has made, because the profile is only
    pushed to S3 when the turn ends. ``restart_session`` asks for that same idle stop with no
    spec change -- a boot-secret input (tokens, gate bearer) changed under a warm session.

    A CONFIRMED stop (or AgentCore reporting no session) is the one point at which superseded
    gate bearers are expired: the next session reads the current boot secret, so nothing can
    present them any more. A busy session keeps them alive.

    Returns ``"current"`` (nothing to do), ``"would-update"`` (dry run), ``"updated"``,
    ``"updated-session-busy"`` -- the runtime IS on the desired spec but its session was not replaced
    by the deadline (a turn still running, a stop AgentCore never confirmed, or a microVM older than the
    update still answering: ``_stop_when_idle``), so an old-spec VM may still serve turns --
    or ``"update-in-progress"``: a concurrent roll's ``UpdateAgentRuntime`` holds the runtime
    (ConflictException), and that roll owns the stop.
    Raises ``ValueError`` for a row with no provisioned runtime.

    ``respec=True`` (the script's ``--respec``, a managed-model flip) also rewrites the profiles
    once the session is idle and stopped: the model lives in the env AND each profile (bridge
    ``sync.py`` refuses a mismatch). A busy owner comes back ``updated-session-busy`` with its
    env moved and its profile NOT -- it breaks on its next cold start unless re-run."""
    from botocore.exceptions import ClientError

    from core.services.runtime_transport import runtime_arn

    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    arn = runtime_arn(row)
    if not arn:
        raise ValueError(f"roll_runtime_image: owner={owner_id} has no provisioned runtime")
    if not settings.AGENTCORE_HERMES_IMAGE:
        raise RuntimeError("AGENTCORE_HERMES_IMAGE is not configured for this environment")
    if arn == settings.HERMES_SHARED_RUNTIME_ARN or arn == row.get("agentcore_shared_runtime_arn"):
        # CDK owns the shared runtime's spec (the image pin and the tier env), and a deploy moves
        # it. What is left per owner is the session: a warm one that predates that update (or still
        # on the workspace's own runtime, at the cut-over) runs the old version until it is stopped.
        arn, updated_at = await _shared_session_to_roll(owner_id, row, arn)
        drifted = updated_at is not None
        if not (drifted or restart_session or respec):
            return "current"
        if dry_run:
            return "would-update"
        return await _stop_rolled_session(
            owner_id,
            arn,
            respec=respec,
            env_drifted=False,
            drifted=drifted,
            restart_session=restart_session,
            current=None,
            updated_at=updated_at,
        )
    from core.billing.bedrock_aip import ensure_owner_bedrock_role
    from core.services.container_provision import resolve_provider_choice

    role_arn = await ensure_owner_bedrock_role(owner_id)
    provider_choice, _ = await resolve_provider_choice(owner_id)
    # The roll is how an ALREADY-provisioned workspace gets its first Browser profile: a
    # create-or-adopt, then the env block.
    browser_profile_id = await _browser_profile_id(owner_id, row)
    # Grant before the env names the memory: a runtime told AGENTCORE_MEMORY_ID must be able to use it.
    from core.billing.bedrock_aip import ensure_owner_memory_policy

    await ensure_owner_memory_policy(owner_id)
    request = build_runtime_request_for_owner(
        owner_id,
        role_arn,
        browser_profile_id=browser_profile_id,
        provider_choice=provider_choice,
        max_concurrent_turns=row.get("max_concurrent_turns"),
        network_workers=bool(row.get("network_workers")),
    )
    actual = await run_in_thread(_runtime_description, arn)
    current = _image_of(actual)
    env_drifted = _env_drifted(actual.get("environmentVariables"), request.get("environmentVariables"))
    # A lifecycle change (IDLE_TIMEOUT_S) on an unchanged image is drift too, or it would reach only new runtimes.
    want_life = request.get("lifecycleConfiguration") or {}
    have_life = actual.get("lifecycleConfiguration") or {}
    life_drifted = any(have_life.get(k) != v for k, v in want_life.items())
    drifted = current != settings.AGENTCORE_HERMES_IMAGE or env_drifted or life_drifted
    if not (drifted or restart_session or respec):
        return "current"
    if dry_run:
        return "would-update"
    updated_at = time.monotonic()
    if drifted:
        try:
            await run_in_thread(_update_runtime_image, arn, request)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConflictException":
                raise
            logger.info("roll_runtime_image: owner=%s runtime already updating -- re-check later", owner_id)
            return "update-in-progress"
        # Optimistic: no READY wait. A single status read still sweeps a dead
        # runtime (UPDATE_FAILED never becomes usable); anything else — CREATING,
        # UPDATING, READY — is left to retry-at-use at first invoke.
        if (await run_in_thread(_runtime_status, arn)) in _TERMINAL_STATUSES:
            raise AgentCoreRuntimeCreateFailed(f"AgentCore runtime {arn} failed to update")
    return await _stop_rolled_session(
        owner_id,
        arn,
        respec=respec,
        env_drifted=env_drifted,
        drifted=drifted,
        restart_session=restart_session,
        current=current,
        updated_at=updated_at if drifted else None,
    )


async def _shared_session_to_roll(owner_id: str, row: dict, shared: str) -> tuple[str, float | None]:
    """Where the owner's warm session runs, and when (monotonic) the version it must not predate
    landed -- None: nothing to roll. On the pool that is the runtime's own ``lastUpdatedAt`` (CDK's
    deploy, not this process). At the cut-over a session still warm on the workspace's OWN runtime
    is old by definition: stopped at idle, its next invoke starts on the pool (``live_arn``)."""
    from core.services.runtime_transport import LIVE_GRACE_S

    session = await run_in_thread(container_repo.get_session_sync, owner_id)
    if int(session.get("warm_until") or 0) <= time.time() - LIVE_GRACE_S:
        return shared, None  # parked: its next start is on the current version
    own = row.get("agentcore_runtime_arn")
    if session.get("arn") not in {own, shared}:
        return shared, None
    description = await run_in_thread(_runtime_description, shared)
    updated = description.get("lastUpdatedAt") or description.get("createdAt")
    if not updated:
        raise RuntimeError("shared runtime has no deployment timestamp; cannot verify rolled session")
    updated_at = time.monotonic() - (time.time() - updated.timestamp())
    if own and own != shared and session.get("arn") == own:
        return own, updated_at  # old runtime must stop; the answering pool VM need only postdate its deploy
    if int(session.get("started_at") or 0) >= updated.timestamp():
        return shared, None
    return shared, updated_at


async def _stop_rolled_session(
    owner_id: str,
    arn: str,
    *,
    respec: bool,
    env_drifted: bool,
    drifted: bool,
    restart_session: bool,
    current,
    updated_at: float | None,
) -> str:
    """``roll_runtime_image``'s tail: the idle stop that moves the session onto the new spec.
    ``updated_at``: the monotonic time of the update an answering microVM must not predate."""
    # Read before the idle probe, which itself wakes a parked session.
    was_warm = await _session_warm(owner_id, arn)
    idle = await _stop_when_idle(owner_id, arn, was_warm=was_warm, updated_at=updated_at, write=respec or env_drifted)
    if not idle:
        logger.warning(
            "roll_runtime_image: owner=%s updated but the session was not replaced within %.0fs (a turn still "
            "running, an unconfirmed stop, or a microVM older than the update still answering) -- re-run it",
            owner_id,
            ROLL_IDLE_WAIT_S,
        )
    logger.info(
        "roll_runtime_image: owner=%s %s -> %s (drifted=%s restart=%s replaced=%s)",
        owner_id,
        current,
        settings.AGENTCORE_HERMES_IMAGE,
        drifted,
        restart_session,
        idle,
    )
    return "updated" if idle else "updated-session-busy"


async def _billing_inputs(owner_id: str) -> tuple:
    """What a reconcile renders from: (provider_choice, byo_provider, plan)."""
    from core.repositories import billing_repo
    from core.services.container_provision import provider_choice_from_row

    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    return (*provider_choice_from_row(row), (row or {}).get("plan"))


async def reconcile_runtime(owner_id: str) -> str:
    """Make the owner's runtime match billing. ONE idempotent function, safe to run
    concurrently (every step converges at its source), with no provider-specific logic of its
    own: the same steps provisioning runs, then the spec check.

    1. ``prepare_hermes_workspace`` -- tokens, derived gate bearer, owner role + gate token row,
       OpenRouter key, ChatGPT grant, boot secret.
    2. ``write_profiles`` -- every roster profile from the billing provider (BYO key read back),
       each bumping ``.revision``, which the bridge re-reads on every turn. THAT is how a
       provider switch lands: no session stop, no idle wait.
    3. ``roll_runtime_image`` -- UpdateAgentRuntime only when the image or env block differs,
       and an idle-only session stop only for a boot-time change (spec, or step 1's secret).

    Overlapping reconciles: each renders from the billing it read at its start, so the one that
    FINISHES last would win with possibly stale inputs. Steps 1-2 therefore re-run once more if
    billing moved underneath them.

    A workspace with no live runtime is not reconciled here: the provision funnel creates it,
    and that execution runs this at its end (``provision_runner``), reading the same billing.

    Before any of that, ``_reconcile_entitlements`` -- the billing-derived duties that live
    outside the runtime (Metronome customer, nightly CEO) -- so a Stripe re-entitlement reaches
    them however the runtime half goes.

    Returns ``"dispatched"``, ``"failed"``, or ``roll_runtime_image``'s answer. A failure NEVER
    flips the running row to error -- the old runtime keeps serving; it is logged and counted
    on ``provision.reconcile.failed`` (paged). A caller retries by calling this again.

    A Metronome ensure failure also answers ``"failed"`` -- AFTER the runtime work, which it does
    not block -- so a reconcile execution is retried by the machine (a credit-drawing payer with
    no ledger is blocked by the credits gate). The nightly CEO arm stays best-effort."""
    metronome_ok = await _reconcile_entitlements(owner_id)
    result = await _reconcile_runtime_to_billing(owner_id)
    return result if metronome_ok else "failed"


async def _reconcile_runtime_to_billing(owner_id: str) -> str:
    """``reconcile_runtime``'s runtime half; never raises."""
    try:
        row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
        from core.services.runtime_transport import runtime_arn

        if not (runtime_arn(row) and row.get("status") == "running"):
            from core.services.provision_dispatch import dispatch_provision

            return "dispatched" if await dispatch_provision(owner_id) else "failed"
        inputs = await _billing_inputs(owner_id)
        boot_changed = False
        for _ in range(2):
            boot_changed = await prepare_hermes_workspace(owner_id) or boot_changed
            await write_profiles(owner_id)
            fresh = await _billing_inputs(owner_id)
            if fresh == inputs:
                break
            logger.info("reconcile_runtime: billing moved for owner=%s mid-reconcile -- rendering again", owner_id)
            inputs = fresh
        return await roll_runtime_image(owner_id, restart_session=boot_changed)
    except Exception:  # noqa: BLE001 -- the old runtime keeps serving; the metric pages
        logger.exception("reconcile_runtime failed for owner=%s", owner_id)
        put_metric("provision.reconcile.failed")
        return "failed"


async def _reconcile_entitlements(owner_id: str) -> bool:
    """Idempotent, never raises -- each failure is logged + metered. Returns whether the Metronome
    ensure succeeded (``reconcile_runtime`` fails on False so the machine retries); the proactive
    ensure's failure is not reported.

    * Metronome customer: ``ensure_metronome_customer`` owns eligibility (a ``trial_local`` or
      canceled owner gets nothing). Skipped only on a POSITIVE ChatGPT/BYO choice -- inference
      we never bill -- never on absence: under-provisioning reopens uncredited managed spend.
    * Proactive: ``proactive.ensure`` follows today's billing, so a plan change or a re-subscribe
      turns the paying-only automations on or off."""
    from core.billing.metronome_provisioning import ensure_metronome_customer
    from core.repositories import billing_repo
    from core.services import proactive

    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        if account.get("provider_choice") not in ("chatgpt_oauth", "byo_key"):
            await ensure_metronome_customer(owner_id=owner_id)
        metronome_ok = True
    except Exception:  # noqa: BLE001 -- reported: reconcile_runtime answers "failed", the machine retries
        put_metric("metronome.provision.error")
        logger.exception("reconcile: Metronome customer ensure failed for owner=%s", owner_id)
        metronome_ok = False
    try:
        await proactive.ensure(owner_id)
    except Exception:  # noqa: BLE001 -- the daily schedule reconcile ensures it again
        logger.exception("reconcile: proactive ensure failed for owner=%s", owner_id)
    return metronome_ok


class HermesProfileError(RuntimeError):
    """A Hermes profile's owner-level inputs, or a per-agent Paperclip API key
    mint, failed hard (fix round 3, M6-R13). A profile that boots but cannot
    authenticate -- to its own LLM provider or to Paperclip -- is a failed
    install, not a degrade, so this is raised instead of shipping an empty
    credential.

    Subclasses ``RuntimeError`` (not a bare ``Exception``) so
    ``container_repo.classify_provision_error``'s treatment of the
    provisioning-runner path is UNCHANGED: none of that function's branches
    single out ``RuntimeError`` by exact type, so a ``HermesProfileError``
    classifies identically to a plain ``RuntimeError`` (retryable, per its
    "everything else" default) — asserted directly in
    ``test_classify_provision_error_treats_hermes_profile_error_like_runtime_error``.
    Also lets every existing ``pytest.raises(RuntimeError, ...)`` /
    ``except RuntimeError`` in this module and its callers keep working
    unchanged.
    """


@dataclass(frozen=True)
class ProfileInputs:
    """The owner-level Hermes profile writer inputs, sourced IDENTICALLY by
    every caller of ``hermes_profiles.write_agent_profile`` (fix round 3,
    M6-R10). Before this, ``catalog_service._build_and_upload_agent_profile``
    hand-rolled its own version of this sourcing that diverged from
    ``write_profiles``'s: it skipped ``provider_choice_from_row``'s
    ``trial_local`` branch, it returned ``None`` for an unset ``byo_provider``
    on a ``byo_key`` owner instead of the ``"anthropic"`` default this module
    reads, and it degraded a roster-read failure to a manifest-derived
    stand-in agent row instead of hard-failing.

    Per-agent inputs (the Paperclip API key mint, the agent row itself) are
    deliberately NOT here — both callers resolve those per agent, not per
    owner, and mint/lookup them differently (a six-agent fan-out here vs. a
    single freshly-deployed agent in catalog_service).
    """

    provider_choice: str
    byo_provider: str | None
    byo_api_key: str | None
    integrations: dict[str, dict]
    env_vars: dict[str, str]
    # The workspace's texting line and the agent that owns it (the front desk), read once per owner.
    front_desk_agent_id: str | None = None
    front_desk_number: str | None = None

    def front_desk_number_for(self, agent_id: str) -> str | None:
        return self.front_desk_number if agent_id == self.front_desk_agent_id else None


async def resolve_profile_inputs(owner_id: str) -> ProfileInputs:
    """The five owner-level Hermes profile writer inputs (fix round 3, M6-R10)
    — the ONE function every caller of ``write_agent_profile`` sources them
    through, so they can never diverge again.

    Raises ``HermesProfileError`` when ``provider_choice == "byo_key"`` and
    no readable key exists for ``byo_provider or "anthropic"`` — a profile
    with no LLM credential at all is a failed install, not a degrade
    (M6-R11, folded into this shared helper by M6-R10).
    """
    from core.repositories import agent_email_repo
    from core.services.container_provision import resolve_provider_choice
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.key_service import read_byo_key

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    byo_api_key = await read_byo_key(owner_id, byo_provider or "anthropic") if provider_choice == "byo_key" else None
    if provider_choice == "byo_key" and not byo_api_key:
        raise HermesProfileError(f"hermes profile for owner={owner_id}: byo api key read failed")
    integrations = await build_connected_integration_servers(owner_id)
    line = await agent_email_repo.text_line_for_owner(owner_id)
    return ProfileInputs(
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        byo_api_key=byo_api_key,
        integrations=integrations,
        env_vars={},  # the Stripe Projects rail that landed these is deleted
        front_desk_agent_id=line.get("agent_id") if line else None,
        front_desk_number=line["address"] if line else None,
    )


async def agent_gate_bearer(owner_id: str, agent_id: str) -> str | None:
    """This agent's own openrouter-gate bearer, for its profile ``.env`` (metering attributes
    inference to the agent). A row with no id yet (pre-prepare) mints none: the agent then
    presents the owner-level boot bearer."""
    from core.billing.bedrock_gate_tokens import mint_agent_gate_token

    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not (row and row.get("id")):
        return None
    return await mint_agent_gate_token(
        owner_id, agent_id, row_id=row["id"], generation=int(row.get("gate_bearer_generation") or 0)
    )


async def write_profile_for_agent(
    owner_id: str,
    agent_row: dict,
    *,
    soul_md_override: str | None = None,
    inputs: ProfileInputs | None = None,
    publish: bool = True,
    roster_fan_out: bool = False,
) -> None:
    """The per-agent Hermes profile write (M6-R23): owner-level inputs (``resolve_profile_inputs``)
    -> ``write_agent_profile`` -> ``upload_profile``.

    Moved, unchanged in behaviour, from ``catalog_service._build_and_upload_agent_profile`` --
    this is now the ONE per-agent "write this agent's profile" seam every caller shares: the
    roster fan-out below (``write_profiles``), a single marketplace-deploy install
    (``catalog_service._build_and_upload_agent_profile``, now a thin resolve-then-delegate
    wrapper), and ``runtime_rpc``'s ``agents.create`` door for an AgentCore owner (Q9a row #1).

    ``agent_row`` is the caller's OWN already-resolved Paperclip roster entry (the shape
    ``write_agent_profile`` documents: ``{"id","name","role","title","capabilities","icon"}``) --
    this function does no roster read of its own, so a caller iterating a roster it already holds
    (``write_profiles``, below) never re-reads it per agent.

    Raises ``HermesProfileError`` when ``resolve_profile_inputs`` hard-fails (a missing BYO key). A ``ValueError`` from
    ``write_agent_profile`` (an illegal dotenv value) is re-raised as a named ``RuntimeError`` so a
    bad integration/env value fails with a diagnosable, agent-scoped message instead of leaving a
    roster half profiled with no indication which agent or variable broke it.

    ``soul_md_override``, when given, replaces the derived SOUL.md (``write_agent_profile``'s own
    ``build_soul_md(agent_row)``) verbatim -- for a caller staging identity text that isn't purely
    roster-derived. ``None`` (the default) writes the derived one; ``write_profiles`` passes the
    stored SOUL.md here when it is owner-edited, so a re-render keeps it.

    ``inputs``, when given, is used AS-IS instead of calling ``resolve_profile_inputs`` again --
    Q9a fix round 1, I3: ``write_profiles``'s six-agent roster fan-out resolves the SAME
    owner-level inputs ONCE and passes them to every agent, rather than re-reading billing/
    Secrets/integrations/env six times (and risking them diverging mid-fan-out if a value
    changes between agents). Every other caller (a single freshly-created/deployed agent) leaves
    this ``None`` and gets its own fresh resolve, unchanged.

    Minor (predeploy review, M6-R39 housekeeping): also stages ``lightbulb-facts.json`` for
    THIS agent (via ``_stage_owner_facts``) -- before this fix only the six-agent provision
    fan-out staged it (as one batched write for the whole roster), so a hired/created agent
    installed through ``catalog_service``'s single-agent path or ``runtime_rpc``'s
    ``agents.create`` door had no facts file at all until the next billing event re-wrote it.
    Best-effort by construction (``_stage_owner_facts`` never raises) -- a facts-file hiccup
    must never fail a profile write that already succeeded.
    """
    from core.services.hermes_profiles import upload_profile, write_agent_profile

    agent_id = agent_row["id"]
    # A roster fan-out passes ``inputs``; a lone create/hire/deploy does not, and that one must
    # also refresh the OTHER agents' facts so their `teammates` list gains the newcomer.
    lone_agent = inputs is None
    if inputs is None:
        inputs = await resolve_profile_inputs(owner_id)
    try:
        files = write_agent_profile(
            owner_id,
            agent_row,
            gate_bearer=await agent_gate_bearer(owner_id, agent_id),
            provider_choice=inputs.provider_choice,
            integrations=inputs.integrations,
            env_vars=inputs.env_vars,
            byo_provider=inputs.byo_provider,
            byo_api_key=inputs.byo_api_key,
            front_desk_number=inputs.front_desk_number_for(agent_id),
        )
    except ValueError as exc:
        raise RuntimeError(
            f"hermes profile for owner={owner_id} agent={agent_id} could not be rendered: {exc}"
        ) from exc
    if soul_md_override is not None:
        files.soul_md = soul_md_override
    await upload_profile(owner_id, agent_id, files)
    if not roster_fan_out:  # ``write_profiles`` runs at provision, before any connect; every other writer comes later
        from core.services.credentials import hermes_cli

        await hermes_cli.seed_new_profile(owner_id, agent_id)  # never raises
    await _stage_owner_facts(owner_id, [agent_id], inputs, refresh_roster=lone_agent)
    if publish:  # ``write_profiles`` publishes once, after its whole fan-out
        from core.services import network_member

        await network_member.publish_sessions(owner_id)  # never raises


async def refresh_agent_soul(owner_id: str, agent_id: str, by: str | None = None) -> None:
    """Rebuild ONE agent's ``SOUL.md`` from the live Paperclip roster row and upload only that
    file — the rename/retitle half of an AgentCore agent's profile.

    Moved here from ``runtime_rpc._refresh_agentcore_soul`` (M6-R59) because the Teams BFF's
    ``PATCH /teams/agents/{id}`` is a SECOND door onto the same change, and the generated-marker
    rule below must have exactly one owner.

    Identity (name/title/role/capabilities) reaches a Hermes agent ONLY through ``SOUL.md``
    (``hermes_profiles.build_soul_md``). Never ``write_profile_for_agent``/``upload_profile``
    here: those also overwrite the owner's own ``config.yaml``/``.env``.

    M6-R48: regenerate ONLY while the stored file is still the backend-generated one — its first
    line is ``hermes_profiles.SOUL_GENERATED_MARKER``. The Instructions tab strips that marker when
    an owner saves ``SOUL.md``, and a migrated/marketplace profile carries text that was never
    generated here at all, so in both cases the file is the owner's and a rename must leave it
    alone. An absent or EMPTY ``SOUL.md`` is still written: there is nothing to preserve
    (``is_generated_soul("")`` is False, so treating "" as owner content would permanently freeze
    regeneration for an agent whose write was half-finished).

    The marker is read as late as possible — AFTER the roster fetch, immediately before the
    upload — so the window in which a concurrent Instructions-tab save can be overwritten is one
    S3 round trip rather than the seconds ``load_agent_roster``'s HTTP call takes. It is a
    narrower race, not a closed one: closing it needs a conditional PUT (``IfMatch`` on the ETag
    this read saw), which ``upload_soul_md`` does not carry today.
    """
    from core.repositories import agent_email_repo
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    roster = await load_agent_roster(owner_id)
    agent_row = next((row for row in roster if isinstance(row, dict) and row.get("id") == agent_id), None)
    if agent_row is None:
        return
    current = await run_in_thread(hermes_profiles.read_soul_md, owner_id, agent_id)
    if not current or hermes_profiles.is_generated_soul(current):
        line = await agent_email_repo.text_line_for_owner(owner_id)
        number = line["address"] if line and line.get("agent_id") == agent_id else None
        soul_md = hermes_profiles.build_soul_md(agent_row, front_desk_number=number)
        await run_in_thread(hermes_profiles.upload_soul_md, owner_id, agent_id, soul_md)
    # Every rename door, owner-edited SOUL.md or not: VOICE_NOTES.md carries the current name,
    # room member labels follow it, and an open Live window hears about it (C3 ``renamed``).
    # Best effort: the Paperclip rename already landed, so a blip here must not 5xx a door and
    # make the person repeat a rename that happened.
    from core.services import voice_calls
    from core.services.messaging import refresh_agent_member_names

    name = agent_row.get("name")
    try:
        _, notes = hermes_profiles.parse_voice_notes_md(
            await run_in_thread(hermes_profiles.read_voice_notes, owner_id, agent_id)
        )
        await run_in_thread(
            hermes_profiles.upload_voice_notes, owner_id, agent_id, hermes_profiles.build_voice_notes_md(name, notes)
        )
        old_names = await refresh_agent_member_names(owner_id, agent_id, roster)
        changed = {room: old for room, old in old_names.items() if old != name}
        if changed:
            await voice_calls.send_renamed_notices(owner_id, agent_id, name, by, changed)
    except Exception:  # noqa: BLE001
        logger.warning("rename follow-ups failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
    # Every rename door (Teams PATCH, agents.update, voice profile edit) lands here, so this is the
    # one place the `teammates` manifest in every agent's facts file learns the new name/role.
    from core.services.owner_facts import write_owner_facts

    await write_owner_facts(owner_id)  # best-effort, never raises
    from core.services import network_member

    await network_member.publish_sessions(owner_id)  # the agent's network address follows the name


async def write_profiles(owner_id: str) -> None:
    """Step 1's profile fan-out: one Hermes profile (config.yaml + .env + SOUL.md) per starter
    agent, uploaded to S3.

    Roster via ``core.services.messaging.load_agent_roster`` (M4-R10) — the same normalized,
    UUID-validated shape ``write_agent_profile``'s ``agent`` parameter expects.

    M6-R23: the per-agent gate/write/upload now lives in ``write_profile_for_agent`` — the same
    function ``catalog_service``'s single-agent install and ``runtime_rpc``'s ``agents.create``
    door for an AgentCore owner call, so there is exactly one place that mints an agent's
    Paperclip key, writes its profile, and uploads it.

    Q9a fix round 1, I3: owner-level inputs are resolved ONCE here and threaded into every
    agent's ``write_profile_for_agent`` call via its ``inputs`` parameter — not once per agent
    (six billing/Secrets/integrations/env reads for a six-agent starter roster, and a risk that
    the roster diverges on those inputs mid-fan-out).

    Minor (predeploy review, M6-R39 housekeeping): ``lightbulb-facts.json`` staging moved INTO
    ``write_profile_for_agent`` itself, so every agent gets it from that one shared per-agent
    call now (not only a roster fan-out's own batched write) — see its docstring. What used to
    be a single combined ``_stage_owner_facts`` call for the whole roster is now one per agent;
    see ``owner_facts.write_owner_facts``'s docstring for why this must happen here rather than
    through that function directly (it has no AgentCore branch of its own).
    """
    from core.services import hermes_profiles
    from core.services.messaging import load_agent_roster

    inputs = await resolve_profile_inputs(owner_id)
    roster = await load_agent_roster(owner_id)
    if not roster:
        # An empty roster used to write nothing and return success, so a reconcile reported
        # 'updated' on a workspace whose profiles never moved (prod org_3JKLW…, 2026-09-23: no
        # Paperclip company, agents stuck on a retired provider). Every workspace is seeded
        # before its profiles are written, so empty means broken -- fail loudly (reconcile
        # returns 'failed' and pages provision.reconcile.failed).
        raise HermesProfileError(f"hermes profile for owner={owner_id}: agent roster is empty")
    for agent in roster:
        # M6-R48 on the re-render path (PATCH /debug/provision, a provider switch): a SOUL.md
        # without the generated marker is the owner's, so carry it through verbatim -- the same
        # rule ``refresh_agent_soul`` applies to a rename. Absent/empty/generated is regenerated.
        current = (
            await run_in_thread(hermes_profiles.read_soul_md, owner_id, agent["id"])
            if settings.OWNER_STATE_BUCKET
            else None
        )
        keep = current if current and not hermes_profiles.is_generated_soul(current) else None
        await write_profile_for_agent(
            owner_id, agent, soul_md_override=keep, inputs=inputs, publish=False, roster_fan_out=True
        )

    from core.services import network_member

    await network_member.publish_sessions(owner_id)


async def _stage_owner_facts(
    owner_id: str, agent_ids: list[str], inputs: ProfileInputs, *, refresh_roster: bool = False
) -> None:
    """Fix round 1, Important 2: a freshly provisioned workspace never ran the ECS config
    writer (the only caller ``owner_facts.write_owner_facts`` ever had), so it had NO
    ``lightbulb-facts.json`` at all while the bundled skill tells every
    agent to read it. Stages the SAME facts file directly into every roster profile instead of
    calling ``write_owner_facts`` itself: at this point in provisioning the row's
    ``runtime_host`` has NOT yet flipped to ``"agentcore"`` (``write_profiles`` runs BEFORE
    ``CreateAgentRuntime`` records the ARN -- N4/M4-R42, above), so ``write_owner_facts``'s own
    a host check would take the wrong branch here and write nothing
    anyone will ever read.

    Sources plan/provider the way that writer's own ``write_owner_facts`` call did
    (as ``account_overrides``): this provision's OWN
    just-resolved ``inputs.provider_choice``/``inputs.byo_provider``, not a re-read that could
    race an in-flight billing write. Email + provisioning-workflow lookups reuse
    ``owner_facts``'s own private helpers so this can never diverge from what the ECS path
    renders. Best-effort, matching ``write_owner_facts``'s own contract -- a facts-file hiccup
    must never fail provisioning.

    ``refresh_roster`` also writes the file to every OTHER agent already in the roster (a lone
    create/hire/deploy changed the `teammates` list they carry). The roster fan-out leaves it
    off: its later agents have no profile yet, and a facts write would half-create one."""
    import json

    from core.repositories import billing_repo
    from core.services import hermes_profiles
    from core.services.owner_facts import (
        _lookup_connected_integrations,
        _lookup_email_address,
        _lookup_owner_timezone,
        _lookup_teammates,
        _lookup_provisioning,
        build_owner_facts,
    )

    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        account = {**account, "provider_choice": inputs.provider_choice, "byo_provider": inputs.byo_provider}
        roster = None
        if refresh_roster:
            from core.services.messaging import load_agent_roster

            roster = await load_agent_roster(owner_id)
            agent_ids = list(dict.fromkeys([*agent_ids, *(a["id"] for a in roster)]))
        facts = build_owner_facts(
            owner_id,
            account,
            email_address=await _lookup_email_address(owner_id),
            provisioning=await _lookup_provisioning(owner_id),
            owner_timezone=await _lookup_owner_timezone(owner_id),
            teammates=await _lookup_teammates(owner_id, roster),
            connected_integrations=await _lookup_connected_integrations(owner_id),
        )
        facts_json = json.dumps(facts, indent=2) + "\n"
        await run_in_thread(hermes_profiles.write_facts_to_profiles, owner_id, agent_ids, facts_json)
    except Exception:  # noqa: BLE001 — a facts-file hiccup must never fail provisioning
        logger.exception("hermes profile facts staging failed for owner=%s", owner_id)


async def provision_hermes_runtime(owner_id: str) -> str:
    """Join the canonical pool without creating or adopting a per-owner runtime.

    Preparation owns existing credentials and profiles. A missing pool is configuration
    failure, never a reason to resurrect the retired per-owner provisioning path.
    """
    if not settings.HERMES_SHARED_RUNTIME_ARN:
        raise RuntimeError("HERMES_SHARED_RUNTIME_ARN is not configured for this environment")
    if not settings.AGENTCORE_HERMES_IMAGE:
        raise RuntimeError("AGENTCORE_HERMES_IMAGE is not configured for this environment")
    if not settings.OWNER_STATE_BUCKET:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured for this environment")
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    return await _join_shared_runtime(owner_id, row)


async def _join_shared_runtime(owner_id: str, row: dict) -> str:
    """``provision_hermes_runtime`` on the tier's shared runtime: nothing to create. The profiles go
    to S3 and the browser profile is recorded, then the row is ``running``; its first invoke starts
    a microVM of its own there (session ids are per owner). No owner role (the session's scoped
    credentials carry S3, memory and the browser), no CreateAgentRuntime. Idempotent like the
    per-owner path: a ``running`` row returns as-is, and a workspace that already has its own
    runtime keeps it (unused while the setting is set) for the per-owner cleanup."""
    shared = settings.HERMES_SHARED_RUNTIME_ARN or row["agentcore_shared_runtime_arn"]
    if row.get("status") == "running":
        if row.get("agentcore_shared_runtime_arn") != shared:
            await container_repo.update_fields(owner_id, {"agentcore_shared_runtime_arn": shared})
        return shared
    await _browser_profile_id(owner_id, row)  # records browser_profile_id: the boot header names it
    if not row.get("agentcore_runtime_arn"):
        await write_profiles(owner_id)  # as the per-owner path: never over a workspace that has them
    await container_repo.update_fields(
        owner_id, {"runtime_host": "agentcore", "agentcore_shared_runtime_arn": shared, "status": "running"}
    )
    logger.info("hermes workspace on the shared runtime owner=%s", owner_id)
    return shared
