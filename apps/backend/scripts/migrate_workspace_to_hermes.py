"""M6 lane R1 — per-workspace migration: ECS-hosted OpenClaw workspace -> Hermes on AgentCore.

This is the job that moves every EXISTING workspace off ECS. Idempotent and resumable are the
whole point: a fleet-wide run (``--all``, Task 3) retries a transient failure once and moves on,
and re-running ``--owner-id`` for an owner who already failed partway must not double-provision
or double-charge. It reuses the SAME fresh-provision primitives ``scripts/provision_runner.py``
uses for a brand-new Hermes workspace — ``core.services.hermes_provisioning.prepare_hermes_workspace``
and ``.provision_hermes_runtime`` — both called with ``migrate=True`` (M4-R37/M4-R39): without it,
both REFUSE a row that still carries an ECS ``service_name``, which is the correct behaviour for
every OTHER caller and exactly wrong for this one.

Ruling M6-R20(c): this job runs INSIDE the backend ECS task (the EFS mount + the backend's own
settings/env) — never from an operator's laptop. ``_preflight_settings``/the EFS-directory check
(Ruling M6-R20a/b) exist precisely to fail loudly and instantly in the wrong environment, rather
than silently "migrating" an owner with zero EFS files or letting ``provision_hermes_runtime``'s
own less friendly ``RuntimeError`` be the first sign of a misconfigured one.

Ordering invariant (Ruling M6-R1): the ECS service is stopped ONLY after the AgentCore runtime is
confirmed READY (``provision_hermes_runtime``'s own internal poll) — never before, and never
concurrently — so two runtimes never serve one owner's EFS-adjacent state at once.

EFS -> Hermes profile mapping (spec §4.8), applied per roster agent:

    EFS (``workspace-{agentId}/``)     Hermes profile (``<owner>/hermes/<agentId>/``)
    --------------------------------   -----------------------------------------------
    SOUL.md                            SOUL.md (via ``write_agent_profile``'s
                                        ``soul_md_override`` — verbatim, not regenerated)
    AGENTS.md                          AGENTS.md (verbatim copy)
    MEMORY.md                          MEMORY.md (verbatim copy)
    memory/**                          memory/** (verbatim copy, same relative paths)
    skills/**                          skills/** (verbatim copy, same relative paths)
    (nothing else)                     config.yaml, .env — generated fresh by the writer,
                                        never read from EFS (spec §4.5: profiles carry no
                                        inherited secrets)

Deliberately NEVER carried (spec §4.8): ``agents/{agentId}/`` OpenClaw RUNTIME state (SQLite
session/transcript DBs, credential/auth tokens) — Hermes has no analogous on-disk session store
to import into, room history replays from DynamoDB's canonical ``messaging`` table on every turn
(see CLAUDE.md's Messaging section), and carrying a live credential file into a profile that
``GET /company/.../profile`` can echo back to the settings UI (M4-R1) would leak it. This mirrors
``core/services/agent_export.py``'s own workspace-vs-agents-dir split for the same reason.

Profile-write ordering (Ruling M6-R14 — ACCEPTED): ``provision_hermes_runtime`` (READY + the
fenced row flip) runs BEFORE the per-agent EFS-content pass (SOUL.md/AGENTS.md/MEMORY.md/
memory/**/skills/** + a ``.revision`` bump the bridge's ``ensure_profile`` re-pull picks up on
the workspace's next turn), which in turn runs before the ECS stop — because
``provision_hermes_runtime``'s own internal ``_write_profiles`` fan-out runs unconditionally on a
no-ARN row and would otherwise clobber an earlier override. The resulting window — vanilla
``build_soul_md`` identity for the seconds between READY and this job's own override upload — is
accepted.

Ruling M6-R15: the ECS stop passes ``force=True`` to ``ecs_manager.stop_user_service`` — the ONE
sanctioned bypass of its ``_hosted_on_agentcore`` no-op guard, since by the time this job calls it
the row's ``runtime_host`` already reads "agentcore" (this job's own terminal write two steps
earlier), which would otherwise make the call a no-op forever and leave the retired ECS task
running and billing.

Ruling M6-R16: the tail (EFS overrides + the forced ECS stop) is RE-ENTERABLE, and its own
completion is stamped. The job's LAST fenced row write sets ``hermes_migrated_at`` (ISO-8601
UTC) — see ``_stamp_migrated``. A row already carrying that stamp is precondition-failed (exit 2,
"already migrated"); a row with no stamp but ``runtime_host == "agentcore"`` (a prior attempt
that crashed after READY but before the tail finished) runs ONLY the tail, skipping
``prepare_hermes_workspace``/``provision_hermes_runtime`` entirely; every other row runs the full
path. ``migrate_all`` follows the same rule, so a fleet-wide re-run picks up exactly the
unfinished tails without re-provisioning anything.

Exit codes (Ruling M6-R6) — the ``--all`` loop (``migrate_all``, Task 3) retries an 11 once:
    0   ok — migrated (or, under --dry-run, the plan printed cleanly)
    1   anything else — an unclassified or non-retryable failure during migration. A PRE-FLIP
        failure (claim/prepare/provision, before the row's runtime_host flips to agentcore —
        Ruling M6-R22) releases the row with status="running" restored (the ECS container is
        still serving) and the failure recorded on hermes_migrate_error/hermes_migrate_exit_code
        — see ``_release_pre_flip_failure``. A POST-flip (tail) failure touches the row not at
        all and relies on Ruling M6-R16's re-entry.
    2   precondition failed — no container row, already migrated (``hermes_migrated_at`` stamped),
        no active Paperclip company, or an empty agent roster
    3   reference org (``REFERENCE_ORG_ID`` / Ruling M6-R2) — refused outright, never migrated
        until every other workspace is confirmed
    10  lost claim — ``ProvisionSuperseded``: another actor (a deliberate reprovision, an admin
        action) took the row's lease out from under this run before or during a leased write;
        nothing this run attempted landed
    11  transient AWS/infra error — retry-able; ``migrate_all`` retries exactly once

Prod-dispatch precondition (the fleet-wide ``--all`` run only targets prod once the Hermes-carrying
backend + CDK are actually live there — CLAUDE.md's "queue: max" section, reused verbatim, not
reimplemented here):

    gh run list --repo TheLightbulbCompany/lightbulb --workflow deploy.yml --limit 10 \\
      --json status --jq '[.[]|select(.status!="completed")]|length'
    # Dispatch the promotion only when this reads 0.
    gh workflow run deploy.yml --ref main

Ruling M6-R12 (ChatGPT OAuth owners): this job never stages a ChatGPT grant onto the new runtime.
The DDB copy of an owner's OAuth tokens is a connect-time SEED — the ECS container's own OpenAI
client rotates the refresh token in place on first use, and OpenAI refresh tokens are single-use,
so the DDB copy is spent the moment the ECS container ever ran a turn. Staging that spent token
into the Hermes runtime risks revoking the WHOLE token family (OpenAI's own re-use detection).
Instead: delete the stale grant so ``core.services.provision_gate``'s ``oauth_required`` check
(``core.services.oauth_service.has_active_oauth``) fires and the owner reconnects once, cleanly,
on their next visit. Reported as ``reconnect_chatgpt`` in both the plan output and the migration
result — never silently.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from core.config import settings
from core.containers import get_ecs_manager, get_workspace
from core.repositories import container_repo
from core.repositories.container_repo import PASS_KEY_PREFIX, ProvisionSuperseded, classify_provision_error
from core.repositories.paperclip_repo import PaperclipRepo
from core.dynamodb import run_in_thread, utc_now_iso
from core.services import runtime_transport
from core.services.container_provision import resolve_provider_choice
from core.services.hermes_profiles import profile_prefix, upload_profile, write_agent_profile
from core.services.hermes_provisioning import prepare_hermes_workspace, provision_hermes_runtime
from core.services.messaging import load_agent_roster

logger = logging.getLogger(__name__)

#: Ruling M6-R2 — the reference org stays on ECS until every other workspace has been confirmed
#: on Hermes. ONE constant: both the single-owner precondition check and the ``--all`` loop
#: import this, never a re-typed string literal.
REFERENCE_ORG_ID = "org_3JCpE2dm4G8Dip7z5e6Lod5fqVY"

#: Exit-code ABI (Ruling M6-R6) — see the module docstring for what each means.
EXIT_OK = 0
EXIT_OTHER = 1
EXIT_PRECONDITION = 2
EXIT_REFERENCE_ORG = 3
EXIT_LOST_CLAIM = 10
EXIT_TRANSIENT = 11

#: Ruling M6-R16 — which leg of the job a row needs. _MODE_FULL provisions from scratch;
#: _MODE_TAIL means a prior attempt already reached READY (row on agentcore, no stamp yet) and
#: only the EFS-override/stop-ECS/stamp tail remains.
_MODE_FULL = "full"
_MODE_TAIL = "tail"

_PAPERCLIP_TABLE = "paperclip-companies"
_SOUL_FILE = "SOUL.md"
#: The exact EFS -> Hermes profile carry list (see the module docstring's mapping table). NEVER
#: sessions/transcripts/``agent/`` runtime state or auth tokens.
_CARRY_TOP_LEVEL_FILES = ("AGENTS.md", "MEMORY.md")
_CARRY_DIRS = ("memory", "skills")


def _s3_client():
    import boto3

    return boto3.client("s3")


def _agentcore_control_client():
    import boto3

    return boto3.client("bedrock-agentcore-control", region_name=settings.AWS_REGION)


def _agentcore_data_client():
    import boto3

    return boto3.client("bedrock-agentcore", region_name=settings.AWS_REGION)


def _delete_runtime(runtime_arn: str) -> None:
    """``DeleteAgentRuntime``. Sync — call via ``run_in_thread``."""
    client = _agentcore_control_client()
    client.delete_agent_runtime(agentRuntimeId=runtime_arn.rsplit("/", 1)[-1])


def _stop_runtime_session(runtime_arn: str, session_id: str) -> None:
    """``StopRuntimeSession`` (Ruling M4-R16) — must run BEFORE the old bearer generation is
    revoked: a warm AgentCore session caches the bearer it read from the boot secret at
    cold-start, so denying/deleting the token row alone leaves an already-warm session
    presenting a now-dead credential until AWS reaps it on its own. Sync — call via
    ``run_in_thread``."""
    client = _agentcore_data_client()
    client.stop_runtime_session(agentRuntimeArn=runtime_arn, runtimeSessionId=session_id)


def _delete_boot_secret(owner_id: str) -> None:
    """Force-delete the owner's Hermes boot secret (no recovery window — a rolled-back attempt's
    credentials are dead the moment the runtime they were minted for is deleted). Sync — call via
    ``run_in_thread``."""
    import boto3

    from core.services.agentcore_runtime import boot_secret_name

    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    try:
        sm.delete_secret(SecretId=boot_secret_name(owner_id), ForceDeleteWithoutRecovery=True)
    except sm.exceptions.ResourceNotFoundException:
        pass


async def _load_company(owner_id: str):
    return await PaperclipRepo(table_name=_PAPERCLIP_TABLE).get(owner_id)


def _agent_workspace_dir(owner_id: str, agent_id: str) -> Path:
    return get_workspace().user_path(owner_id) / f"workspace-{agent_id}"


def _read_text_if_present(path: Path) -> str | None:
    return path.read_text() if path.is_file() else None


def _collect_carry_files(agent_dir: Path) -> dict[str, Path]:
    """workspace-relative path -> absolute path for everything (besides ``SOUL.md``, carried
    separately as the writer's ``soul_md_override``) this job copies into the Hermes profile."""
    out: dict[str, Path] = {}
    for name in _CARRY_TOP_LEVEL_FILES:
        candidate = agent_dir / name
        if candidate.is_file():
            out[name] = candidate
    for sub in _CARRY_DIRS:
        base = agent_dir / sub
        if not base.is_dir():
            continue
        for f in base.rglob("*"):
            if f.is_file():
                out[str(f.relative_to(agent_dir))] = f
    return out


async def _build_plan(owner_id: str, roster: list[dict]) -> list[tuple[dict, str | None, dict[str, Path]]]:
    plan = []
    for agent in roster:
        agent_dir = _agent_workspace_dir(owner_id, agent["id"])
        plan.append((agent, _read_text_if_present(agent_dir / _SOUL_FILE), _collect_carry_files(agent_dir)))
    return plan


def _print_plan(owner_id: str, plan, *, dry_run: bool, reconnect_chatgpt: bool) -> None:
    tag = "DRY RUN: " if dry_run else ""
    for agent, soul, files in plan:
        file_count = len(files) + (1 if soul else 0)
        print(f"{tag}{owner_id} agent={agent['id']} role={agent.get('role') or 'standard'} efs_files={file_count}")
    if reconnect_chatgpt:
        print(f"{tag}{owner_id} reconnect_chatgpt")


def _preflight_settings() -> list[str]:
    """Ruling M6-R20(b): the settings ``provision_hermes_runtime`` hard-requires (it raises
    ``RuntimeError`` on either being unset), checked ONCE per ``migrate()``/``migrate_all()``
    call, BEFORE any row is read — an unset value must refuse instantly, with the missing
    names, rather than ``--all`` sweeping the whole fleet into that RuntimeError one owner at a
    time. Returns the names that are missing (empty when the preflight passes)."""
    missing = []
    if not settings.AGENTCORE_HERMES_IMAGE:
        missing.append("AGENTCORE_HERMES_IMAGE")
    if not settings.OWNER_STATE_BUCKET:
        missing.append("OWNER_STATE_BUCKET")
    return missing


def _efs_root_present() -> bool:
    """Ruling M6-R20(a), fleet-wide variant: ``migrate_all`` needs to know the EFS mount itself
    is present BEFORE it has any owner to build a ``user_path`` from. ``user_path(id)`` is
    ``mount / id``, so ``.parent`` of any valid (non-empty, slash-free) id's path is the mount
    root itself — this reaches it through the public ``Workspace`` API rather than a private
    attribute, and needs no real owner."""
    return get_workspace().user_path(REFERENCE_ORG_ID).parent.is_dir()


def _tail_eligible(row: dict) -> bool:
    """Ruling M6-R19: tail mode requires an ECS-provenance row — ``service_name`` AND an ARN —
    not merely ``runtime_host == "agentcore"``. A natively provisioned Hermes row (the ordinary
    fresh-provision path, ``settings.HERMES_RUNTIME``) never touches the ECS manager and so has
    no ``service_name``; running the tail on it would regenerate its live profiles over
    themselves for nothing. Shared by ``_check_preconditions`` and ``migrate_all`` so the two
    predicates can never drift apart."""
    return bool(row.get("service_name") and row.get("agentcore_runtime_arn"))


async def _check_preconditions(
    owner_id: str, *, company=None
) -> tuple[int | None, str | None, list[dict] | None, dict | None]:
    """Ruling M6-R1 step 1 / Ruling M6-R16 / Ruling M6-R19 / Ruling M6-R20. Returns
    ``(exit_code, None, None, None)`` on failure, or ``(None, mode, roster, row)`` where ``mode``
    is ``_MODE_FULL`` (provision from scratch) or ``_MODE_TAIL`` (a prior attempt already
    reached READY — the row is an ECS workspace now on agentcore but carries no
    ``hermes_migrated_at`` stamp yet, so only the tail remains). ``row`` is returned so the tail
    can read ``agentcore_runtime_arn`` off it directly in ``_MODE_TAIL`` (Ruling M6-R18) without
    a second read.

    ``company``: when given (``migrate_all`` already read it to decide whether to call
    ``migrate()`` at all), it is used as-is instead of a second ``_load_company`` read for the
    SAME owner — a fleet-wide ``--all`` run must not double every Paperclip-company lookup.
    """
    if owner_id == REFERENCE_ORG_ID:
        logger.error("migrate: %s is the reference org (M6-R2) — refusing", owner_id)
        return EXIT_REFERENCE_ORG, None, None, None
    missing = _preflight_settings()
    if missing:
        logger.error("migrate: refusing owner=%s — missing required settings: %s", owner_id, ", ".join(missing))
        return EXIT_PRECONDITION, None, None, None
    if not get_workspace().user_path(owner_id).is_dir():
        logger.error(
            "migrate: no EFS workspace directory for owner=%s — refusing (M6-R20a: this job runs "
            "inside the backend task, never from an operator laptop with no /mnt/efs mount)",
            owner_id,
        )
        return EXIT_PRECONDITION, None, None, None
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not row:
        logger.error("migrate: no container row for owner=%s", owner_id)
        return EXIT_PRECONDITION, None, None, None
    if row.get("hermes_migrated_at"):
        logger.info("migrate: owner=%s is already migrated (hermes_migrated_at stamped)", owner_id)
        return EXIT_PRECONDITION, None, None, None
    if runtime_transport.host_for(row) == "agentcore":
        if not _tail_eligible(row):
            logger.error(
                "migrate: owner=%s is on agentcore but not an ECS workspace (no service_name/ARN) — refusing",
                owner_id,
            )
            return EXIT_PRECONDITION, None, None, None
        mode = _MODE_TAIL
    else:
        mode = _MODE_FULL
    company = company if company is not None else await _load_company(owner_id)
    if not company or company.status != "active":
        logger.error("migrate: owner=%s has no active Paperclip company row", owner_id)
        return EXIT_PRECONDITION, None, None, None
    try:
        roster = await load_agent_roster(owner_id)
    except Exception:  # noqa: BLE001 — a roster fetch failure is Paperclip being unreachable, not ineligibility
        logger.exception("migrate: roster fetch failed for owner=%s — treating as transient", owner_id)
        return EXIT_TRANSIENT, None, None, None
    if not roster:
        logger.error("migrate: owner=%s has an empty agent roster", owner_id)
        return EXIT_PRECONDITION, None, None, None
    return None, mode, roster, row


async def _release_pre_flip_failure(owner_id: str, claim_id: str, attempt_token: str | None, exc: Exception) -> int:
    """Ruling M6-R22 — a claim/prepare/provision failure happens BEFORE the row's
    ``runtime_host`` flips to agentcore, while the owner's ECS container is still up and
    serving. Unlike a fresh Hermes provision's own failure path
    (``provision_runner._release_hermes_failure_to_error``, which sets ``status="error"`` —
    correct THERE, since nothing is running yet), releasing this lease to ``status="error"``
    would flip a row describing a perfectly healthy, still-serving workspace, misleading every
    other reader (the status poll, the frontend's blocked-state banner, an operator glancing at
    the row) into thinking it is broken. So this job never calls
    ``_release_hermes_failure_to_error`` for a pre-flip failure: it releases the lease with
    ``status="running"`` restored and the failure recorded on ``hermes_migrate_error``/
    ``hermes_migrate_exit_code`` (``str(exc)`` only — no secrets) so an operator can see why
    without the row lying about the workspace. A concurrent takeover racing this release is not
    an error — the successor now owns the row and this run has nothing left to correct.
    """
    logger.exception("migrate: pre-flip failure for owner=%s (ECS container still serving)", owner_id)
    _, retryable = classify_provision_error(exc)
    exit_code = EXIT_TRANSIENT if retryable else EXIT_OTHER
    try:
        await container_repo.update_container_leased(
            owner_id,
            claim_id,
            {"status": "running", "hermes_migrate_error": str(exc)[:500], "hermes_migrate_exit_code": exit_code},
            attempt_token=attempt_token,
            release_lease=True,
        )
    except ProvisionSuperseded:
        pass
    return exit_code


async def _apply_efs_overrides(owner_id: str, plan: list[tuple[dict, str | None, dict[str, Path]]]) -> None:
    """Layer the EFS-sourced identity/content on top of the vanilla profiles
    ``provision_hermes_runtime`` just wrote for every roster agent (see the module docstring's
    ordering note). Owner-level inputs via ``hermes_provisioning.resolve_profile_inputs`` (Ruling
    M6-R10, adopted here per Ruling M6-R18 — the formerly-deferred concern 4) — the SAME shared
    resolver ``hermes_provisioning._write_profiles`` uses internally, so this and the internal
    fan-out can never diverge again. ``aip_arns`` is re-derived via ``ensure_owner_aips``
    (idempotent create-or-reuse) rather than trusted off a row read earlier in this job, mirroring
    ``_write_profiles``'s own reasoning: a row read before ``provision_hermes_runtime`` ran would
    be stale.
    """
    from core.billing.bedrock_aip import ensure_owner_aips
    from core.services.hermes_provisioning import resolve_profile_inputs
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_api_key import ensure_paperclip_api_key

    aip_arns = await ensure_owner_aips(owner_id)
    inputs = await resolve_profile_inputs(owner_id, container_row={"bedrock_aip_arns": aip_arns})
    admin_client = shared_admin_client()
    bucket = settings.OWNER_STATE_BUCKET
    s3 = _s3_client() if bucket else None

    for agent, soul, files in plan:
        agent_id = agent["id"]
        paperclip_api_key = await ensure_paperclip_api_key(
            owner_id=owner_id, paperclip_agent_id=agent_id, admin_client=admin_client
        )
        if paperclip_api_key is None:
            raise RuntimeError(f"migrate: paperclip API key mint/read failed for owner={owner_id} agent={agent_id}")
        profile = write_agent_profile(
            owner_id,
            agent,
            provider_choice=inputs.provider_choice,
            aip_arns=inputs.aip_arns,
            paperclip_api_key=paperclip_api_key,
            integrations=inputs.integrations,
            env_vars=inputs.env_vars,
            byo_provider=inputs.byo_provider,
            byo_api_key=inputs.byo_api_key,
            soul_md_override=soul,
        )
        if s3 is not None:
            prefix = profile_prefix(owner_id, agent_id)
            for rel_path, src in files.items():
                await run_in_thread(s3.put_object, Bucket=bucket, Key=f"{prefix}{rel_path}", Body=src.read_bytes())
        # LAST: upload_profile itself writes config.yaml/.env/SOUL.md then bumps .revision last,
        # so the extra carry files above are always in place before the bridge's re-pull signal.
        await upload_profile(owner_id, agent_id, profile)


async def _reconcile_chatgpt_oauth(owner_id: str) -> None:
    """Ruling M6-R12 — see the module docstring. Deletes the stale DDB grant; never stages one."""
    from core.services.oauth_service import resolve_token_owner, revoke_user_oauth

    connector_user_id = await resolve_token_owner(owner_id=owner_id)
    await revoke_user_oauth(user_id=connector_user_id)


async def _stamp_migrated(owner_id: str) -> None:
    """Ruling M6-R16/M6-R17 — the job's LAST fenced write. ``provision_hermes_runtime`` already
    released the lease this job's earlier claim held (its own terminal write uses
    ``release_lease=True``), so there is nothing left to reuse — this re-claims via
    ``takeover_provision`` purely to make this a FENCED write like every other terminal
    transition in this job. Stamps ``hermes_migrated_at`` ALONE (what makes the tail
    re-enterable: a later run's precondition check treats a stamped row as done) — ``status`` is
    NOT written here: ``stop_user_service(force=True)`` no longer touches the row at all
    (M6-R17), so ``status`` is exactly whatever ``provision_hermes_runtime``'s own terminal write
    already set it to ("running"), and re-asserting it here would just be a second writer of a
    value nothing else has touched.
    """
    claim_id = await container_repo.takeover_provision(owner_id)
    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        {"hermes_migrated_at": utc_now_iso()},
        attempt_token=claim_id,
        release_lease=True,
    )


async def migrate(owner_id: str, *, dry_run: bool, _company=None) -> int:
    """``_company``: internal — ``migrate_all`` passes its own already-read Paperclip company
    row through here so ``_check_preconditions`` never reads it a second time for the same
    owner (Ruling M6-R6 minors)."""
    exit_code, mode, roster, row = await _check_preconditions(owner_id, company=_company)
    if exit_code is not None:
        return exit_code

    provider_choice, _ = await resolve_provider_choice(owner_id)
    reconnect_chatgpt = provider_choice == "chatgpt_oauth"
    plan = await _build_plan(owner_id, roster)
    _print_plan(owner_id, plan, dry_run=dry_run, reconnect_chatgpt=reconnect_chatgpt)
    if dry_run:
        return EXIT_OK

    # The runtime ARN this tail needs to force-cold-restart (Ruling M6-R18, below): in
    # _MODE_TAIL it already exists on the row _check_preconditions read (_tail_eligible already
    # confirmed it); in _MODE_FULL it comes back from provision_hermes_runtime itself.
    runtime_arn = row.get("agentcore_runtime_arn") if mode == _MODE_TAIL else None

    if mode == _MODE_FULL:
        try:
            claim_id = await container_repo.takeover_provision(owner_id)
        except ProvisionSuperseded:
            logger.warning("migrate: lost the provision claim for owner=%s before starting", owner_id)
            return EXIT_LOST_CLAIM
        except Exception as exc:  # noqa: BLE001 — a genuine claim failure, not a lost race
            logger.exception("migrate: could not claim owner=%s", owner_id)
            _, retryable = classify_provision_error(exc)
            return EXIT_TRANSIENT if retryable else EXIT_OTHER

        try:
            await prepare_hermes_workspace(owner_id, claim_id=claim_id, attempt_token=claim_id, migrate=True)
        except ProvisionSuperseded:
            logger.warning("migrate: lost the provision claim for owner=%s during prepare", owner_id)
            return EXIT_LOST_CLAIM
        except Exception as exc:  # noqa: BLE001 — pre-flip: the ECS container is still serving (M6-R22)
            return await _release_pre_flip_failure(owner_id, claim_id, claim_id, exc)

        try:
            runtime_arn = await provision_hermes_runtime(
                owner_id, claim_id=claim_id, attempt_token=claim_id, migrate=True
            )
        except ProvisionSuperseded:
            logger.warning("migrate: lost the provision claim for owner=%s during runtime provision", owner_id)
            return EXIT_LOST_CLAIM
        except Exception as exc:  # noqa: BLE001 — pre-flip: the ECS container is still serving (M6-R22)
            return await _release_pre_flip_failure(owner_id, claim_id, claim_id, exc)

    # mode == _MODE_TAIL (a prior attempt already reached here) or mode == _MODE_FULL (just
    # reached it above): run the tail. Ruling M6-R16 — this is RE-ENTERABLE: nothing here
    # depends on the claim/lease the block above held, which provision_hermes_runtime already
    # released on its own terminal write. A failure from here on is a live-workspace cleanup
    # step, not a lost migration, so it is classified rather than treated as a lost claim.
    try:
        await _apply_efs_overrides(owner_id, plan)
        if reconnect_chatgpt:
            await _reconcile_chatgpt_oauth(owner_id)
        # Ruling M6-R18: the bridge's ensure_profile only re-pulls the four backend-owned files
        # (config.yaml/.env/SOUL.md/.revision) on a revision change, and save_profile syncs the
        # AGENT-owned files (memory/**, skills/**, AGENTS.md, MEMORY.md) back FROM the local
        # dir -- so a warm process that took a turn during the brief vanilla window (Ruling
        # M6-R14) would sync vanilla agent-owned content OVER what was just uploaded above.
        # Stopping the runtime session forces the next invoke to cold-restore the whole profile
        # from S3, which is the only way the override actually survives. Must run AFTER the last
        # override upload (so there is something correct to cold-restore) and BEFORE the ECS
        # stop (matches Ruling M6-R1's ordering). Best-effort, exactly like rollback's own use of
        # this helper (fix round 2, item 1): a fresh _MODE_FULL runtime usually has NO warm
        # session yet, so StopRuntimeSession raising "unknown session" here must not abort before
        # the ECS stop and the stamp below -- that would leave the row flipped to agentcore with
        # the old ECS service still running and billing, and --all retrying this owner forever.
        if runtime_arn:
            try:
                await run_in_thread(_stop_runtime_session, runtime_arn, runtime_transport.session_id_for(owner_id))
            except Exception:  # noqa: BLE001 — best-effort: no session to stop is not fatal
                logger.warning(
                    "migrate: StopRuntimeSession failed for owner=%s arn=%s", owner_id, runtime_arn, exc_info=True
                )
        # Order matters (Ruling M6-R1): the ECS service is stopped only now that the AgentCore
        # runtime is confirmed READY, so two runtimes never serve one owner at once. force=True
        # (Ruling M6-R15) bypasses the AgentCore no-op guard -- by now the row already reads
        # runtime_host=agentcore, which would otherwise make this call a no-op forever.
        await get_ecs_manager().stop_user_service(owner_id, reason="hermes_migration", force=True)
        await _stamp_migrated(owner_id)
    except ProvisionSuperseded:
        # _stamp_migrated's own fenced write lost the race to a concurrent takeover -- a genuine
        # lost claim (Ruling M6-R6 minors), not a generic transient failure: classify_provision_error
        # has no special case for this exception and would otherwise call it retryable (11).
        logger.warning("migrate: lost the provision claim for owner=%s during the tail stamp", owner_id)
        return EXIT_LOST_CLAIM
    except Exception as exc:  # noqa: BLE001 — the runtime is already live; safe for --all to retry
        logger.exception("migrate: post-provision step failed for owner=%s (runtime is already live)", owner_id)
        _, retryable = classify_provision_error(exc)
        return EXIT_TRANSIENT if retryable else EXIT_OTHER
    return EXIT_OK


async def _teardown_agentcore(owner_id: str, row: dict) -> None:
    """The AWS-side half of ``rollback`` — everything that undoes what ``migrate`` created.
    Best-effort per step: a runtime or secret that is already gone must not stop the row from
    flipping back to ECS."""
    from core.billing.bedrock_gate_tokens import delete_gate_token_rows

    arn = row.get("agentcore_runtime_arn")
    if arn:
        try:
            await run_in_thread(_stop_runtime_session, arn, runtime_transport.session_id_for(owner_id))
        except Exception:  # noqa: BLE001 — best-effort: an already-cold/gone session is not fatal
            logger.warning("rollback: StopRuntimeSession failed for owner=%s arn=%s", owner_id, arn, exc_info=True)
        try:
            await run_in_thread(_delete_runtime, arn)
        except Exception:  # noqa: BLE001 — best-effort: the runtime may already be gone
            logger.warning("rollback: DeleteAgentRuntime failed for owner=%s arn=%s", owner_id, arn, exc_info=True)

    current_hash = row.get("bedrock_gate_token_hash")
    if current_hash:
        try:
            await delete_gate_token_rows([current_hash])
        except Exception:  # noqa: BLE001 — best-effort: a stranded row is a known, accepted risk elsewhere too
            logger.warning("rollback: could not revoke gate token for owner=%s", owner_id, exc_info=True)

    try:
        await run_in_thread(_delete_boot_secret, owner_id)
    except Exception:  # noqa: BLE001 — best-effort cleanup
        logger.warning("rollback: could not delete boot secret for owner=%s", owner_id, exc_info=True)


async def rollback(owner_id: str, *, dry_run: bool = False) -> int:
    """Ruling M6-R3 — undo a migration in progress or already landed: tear down the AgentCore
    side (session stop THEN delete, per Ruling M4-R16 — see ``_stop_runtime_session``), revoke
    the Hermes-minted bearer generation, delete the boot secret, flip the row back onto ECS
    (fenced, same lease mechanic as ``migrate``), and restart the ECS service. S3 profiles are
    left in place — cheap, and the next ``migrate()`` attempt overwrites them anyway.

    ``dry_run`` performs none of the above — it exists so an operator can run ``--rollback
    --dry-run`` to confirm which owner they are about to target before actually doing it.
    """
    if dry_run:
        print(
            f"DRY RUN: would roll back {owner_id} (tear down its AgentCore runtime, revoke its "
            "minted bearer, delete its boot secret, flip it back onto ECS, restart it) -- nothing done"
        )
        return EXIT_OK
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    await _teardown_agentcore(owner_id, row)

    claim_id = await container_repo.takeover_provision(owner_id)
    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        # hermes_migrated_at cleared with the flip (Ruling M6-R16) -- a rolled-back owner is not
        # "already migrated" any more, and a later migrate() must run the full path again.
        {"runtime_host": "ecs", "agentcore_runtime_arn": None, "status": "stopped", "hermes_migrated_at": None},
        attempt_token=claim_id,
        release_lease=True,
    )
    await get_ecs_manager().start_user_service(owner_id, force=True)
    logger.info("rollback: owner=%s restored to ecs", owner_id)
    return EXIT_OK


async def migrate_all(*, dry_run: bool) -> int:
    """Task 3 — fleet-wide flip for the per-workspace-flip phase, once prod carries the Hermes
    image (see the module docstring's prod-dispatch precondition). Sequential: a fleet-wide
    migration is an operator-watched event, not a background job, and nothing here has load-
    tested AgentCore's per-account ``CreateAgentRuntime`` rate. The founder/reference org is
    never included (Ruling M6-R2) — Task 2 runs it explicitly via ``--owner-id`` first.

    Ruling M6-R16: skips only STAMPED rows (``hermes_migrated_at``) — an unstamped agentcore row
    (a prior attempt that crashed after READY but before its tail finished) is INCLUDED, since
    ``migrate()`` on that row runs only the remaining tail rather than re-provisioning.

    Ruling M6-R19: a row on agentcore that is NOT tail-eligible (``_tail_eligible`` — no
    ``service_name``/ARN, i.e. a natively provisioned Hermes workspace) is also skipped — mirrors
    ``_check_preconditions``'s own refusal so ``--all`` never regenerates a native Hermes owner's
    live profiles over themselves.

    ``_load_company`` is read exactly ONCE per owner here (never again inside ``migrate()`` —
    it is passed through as ``_company``), and its ``reconnect_chatgpt`` status is looked up
    once more per owner purely for the summary table below.

    Ruling M6-R20(b), placement fix (fix round 2, item 2): the settings preflight and the
    EFS-root presence check run ONCE here, before ``scan_all`` and before any Paperclip read —
    a misconfigured environment must refuse instantly with exit 2, not scan the whole table,
    read every owner's company row, and exit 0 having touched nothing (a misleadingly clean
    summary for a run that migrated no one). Single-owner mode keeps its own per-owner checks in
    ``_check_preconditions``.
    """
    missing = _preflight_settings()
    if missing:
        logger.error("migrate_all: refusing — missing required settings: %s", ", ".join(missing))
        return EXIT_PRECONDITION
    if not _efs_root_present():
        logger.error(
            "migrate_all: no EFS mount present — refusing (this job runs inside the backend "
            "task, never from an operator laptop with no /mnt/efs mount)"
        )
        return EXIT_PRECONDITION

    rows = await container_repo.scan_all()
    results: list[tuple[str, int, bool]] = []
    for row in rows:
        owner_id = row.get("owner_id")
        if not owner_id or owner_id.startswith(PASS_KEY_PREFIX) or owner_id == REFERENCE_ORG_ID:
            continue
        if row.get("hermes_migrated_at"):
            continue
        if runtime_transport.host_for(row) == "agentcore" and not _tail_eligible(row):
            continue
        company = await _load_company(owner_id)
        if not company or company.status != "active":
            logger.info("migrate_all: skipping owner=%s — no active Paperclip company", owner_id)
            continue
        code = await migrate(owner_id, dry_run=dry_run, _company=company)
        if code == EXIT_TRANSIENT:
            logger.warning("migrate_all: owner=%s exited transient — retrying once", owner_id)
            code = await migrate(owner_id, dry_run=dry_run, _company=company)
        provider_choice, _ = await resolve_provider_choice(owner_id)
        results.append((owner_id, code, provider_choice == "chatgpt_oauth"))

    tag = "DRY RUN " if dry_run else ""
    print(f"{tag}migrate_all summary:")
    for owner_id, code, reconnect_chatgpt in results:
        suffix = " reconnect_chatgpt" if reconnect_chatgpt else ""
        print(f"  {owner_id}: exit={code}{suffix}")
    # Ruling M6-R6 minors: never max(codes) -- 1 (a real failure) always wins over 11 (merely
    # retry-able), and anything else present with neither is a clean 0. max() would let a
    # meaningless numeral ordering (e.g. 10 > 1) misreport a run that actually failed hard.
    codes = [code for _, code, _ in results]
    if EXIT_OTHER in codes:
        return EXIT_OTHER
    if EXIT_TRANSIENT in codes:
        return EXIT_TRANSIENT
    return EXIT_OK


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner-id")
    target.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    if args.rollback and args.all:
        parser.error("--rollback cannot be combined with --all (rollback takes exactly one owner)")
    if args.rollback:
        if not args.owner_id:
            parser.error("--rollback requires --owner-id")
        return asyncio.run(rollback(args.owner_id, dry_run=args.dry_run))
    if args.all:
        return asyncio.run(migrate_all(dry_run=args.dry_run))
    return asyncio.run(migrate(args.owner_id, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
