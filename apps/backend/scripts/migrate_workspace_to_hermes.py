"""M6 lane R1 — per-workspace migration: ECS-hosted OpenClaw workspace -> Hermes on AgentCore.

This is the job that moves every EXISTING workspace off ECS. Idempotent and resumable are the
whole point: a fleet-wide run (``--all``, Task 3) retries a transient failure once and moves on,
and re-running ``--owner-id`` for an owner who already failed partway must not double-provision
or double-charge. It reuses the SAME fresh-provision primitives ``scripts/provision_runner.py``
uses for a brand-new Hermes workspace — ``core.services.hermes_provisioning.prepare_hermes_workspace``
and ``.provision_hermes_runtime`` — both called with ``migrate=True`` (M4-R37/M4-R39): without it,
both REFUSE a row that still carries an ECS ``service_name``, which is the correct behaviour for
every OTHER caller and exactly wrong for this one.

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

Profile-write ordering (a deliberate deviation from a literal read of the lane brief's prose,
documented here for review): the per-agent EFS-content pass runs AFTER ``provision_hermes_runtime``
returns, not before it. ``provision_hermes_runtime`` unconditionally re-runs its OWN internal
``_write_profiles`` fan-out (vanilla ``build_soul_md`` identity, no EFS content) whenever the row
has no ARN yet — true on every first attempt — so an EFS-content upload placed BEFORE that call
would have its ``SOUL.md`` override silently reverted the instant ``provision_hermes_runtime``
runs. Placing this job's own upload AFTER guarantees it is the last write and therefore the one
actually served; M4-R42 (the internal fan-out is skipped once the row already carries an ARN)
is what makes a RETRY of this function idempotent rather than what makes ordering irrelevant on
attempt 0.

Known gap, surfaced for review rather than silently worked around: ``ecs_manager.stop_user_service``
no-ops once the row's ``runtime_host`` reads "agentcore" (``_hosted_on_agentcore``), which
``provision_hermes_runtime``'s own terminal write sets BEFORE this job calls it. Live traffic is
already routed off ECS by that row flip (``runtime_transport.host_for``), so this is a cost/cleanup
gap (an idle ECS task keeps running) rather than a correctness one, but the call this module makes
per Ruling M6-R1 does not itself scale the old service to zero in production. Flagged, not
worked around, since fixing it means touching ``ecs_manager.py``, which this lane's brief does not
authorize.

Exit codes (Ruling M6-R6) — the ``--all`` loop (``migrate_all``, Task 3) retries an 11 once:
    0   ok — migrated (or, under --dry-run, the plan printed cleanly)
    1   anything else — an unclassified or non-retryable failure during migration; the row has
        already been released to status="error" (see ``_release_and_map``) wherever the failure
        happened inside the prepare/provision steps
    2   precondition failed — no container row, already on agentcore, no active Paperclip
        company, or an empty agent roster
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
from core.dynamodb import run_in_thread
from core.services import runtime_transport
from core.services.container_provision import resolve_provider_choice
from core.services.hermes_profiles import profile_prefix, upload_profile, write_agent_profile
from core.services.hermes_provisioning import prepare_hermes_workspace, provision_hermes_runtime
from core.services.messaging import load_agent_roster
from scripts.provision_runner import EXIT_TRANSIENT as _RUNNER_EXIT_TRANSIENT
from scripts.provision_runner import _release_hermes_failure_to_error

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


async def _check_preconditions(owner_id: str) -> tuple[int | None, list[dict] | None]:
    """Ruling M6-R1 step 1. Returns ``(exit_code, None)`` on failure, or ``(None, roster)``."""
    if owner_id == REFERENCE_ORG_ID:
        logger.error("migrate: %s is the reference org (M6-R2) — refusing", owner_id)
        return EXIT_REFERENCE_ORG, None
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    if not row:
        logger.error("migrate: no container row for owner=%s", owner_id)
        return EXIT_PRECONDITION, None
    if runtime_transport.host_for(row) == "agentcore":
        logger.info("migrate: owner=%s is already on agentcore — nothing to do", owner_id)
        return EXIT_PRECONDITION, None
    company = await _load_company(owner_id)
    if not company or company.status != "active":
        logger.error("migrate: owner=%s has no active Paperclip company row", owner_id)
        return EXIT_PRECONDITION, None
    try:
        roster = await load_agent_roster(owner_id)
    except Exception:  # noqa: BLE001 — a roster fetch failure is Paperclip being unreachable, not ineligibility
        logger.exception("migrate: roster fetch failed for owner=%s — treating as transient", owner_id)
        return EXIT_TRANSIENT, None
    if not roster:
        logger.error("migrate: owner=%s has an empty agent roster", owner_id)
        return EXIT_PRECONDITION, None
    return None, roster


def _map_release_exit(runner_exit_code: int) -> int:
    """Translate ``_release_hermes_failure_to_error``'s ABI (``scripts.provision_runner``'s own:
    0/10/11) onto THIS job's ABI (Ruling M6-R6). Its ``EXIT_TRANSIENT`` (11) means the identical
    thing here and passes through unchanged. Its ``EXIT_DETERMINISTIC`` (10) would collide with
    this job's OWN meaning for 10 ("lost claim" / ``ProvisionSuperseded`` — see the module
    docstring), so a genuine non-retryable prepare/provision failure is reported as
    ``EXIT_OTHER`` (1) instead, never as 10.
    """
    return EXIT_TRANSIENT if runner_exit_code == _RUNNER_EXIT_TRANSIENT else EXIT_OTHER


async def _apply_efs_overrides(
    owner_id: str,
    plan: list[tuple[dict, str | None, dict[str, Path]]],
    *,
    provider_choice: str,
    byo_provider: str | None,
) -> None:
    """Layer the EFS-sourced identity/content on top of the vanilla profiles
    ``provision_hermes_runtime`` just wrote for every roster agent (see the module docstring's
    ordering note). Resolves the SAME provider context ``hermes_provisioning._write_profiles``
    resolves internally, via the same underlying primitives — that private function has no hook
    for a per-agent EFS override and this lane may not edit ``hermes_provisioning.py`` to add one.
    """
    from core.billing.bedrock_aip import ensure_owner_aips
    from core.services.credentials.mcp_server import build_connected_integration_servers
    from core.services.key_service import read_byo_key
    from core.services.paperclip_admin_client import shared_admin_client
    from core.services.paperclip_api_key import ensure_paperclip_api_key
    from core.services.provisioning_landing import build_provisioned_env

    byo_api_key = await read_byo_key(owner_id, byo_provider or "anthropic") if provider_choice == "byo_key" else None
    aip_arns = await ensure_owner_aips(owner_id)
    integrations = await build_connected_integration_servers(owner_id)
    env_vars = await build_provisioned_env(owner_id)
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
            provider_choice=provider_choice,
            aip_arns=aip_arns,
            paperclip_api_key=paperclip_api_key,
            integrations=integrations,
            env_vars=env_vars,
            byo_provider=byo_provider,
            byo_api_key=byo_api_key,
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


async def migrate(owner_id: str, *, dry_run: bool) -> int:
    exit_code, roster = await _check_preconditions(owner_id)
    if exit_code is not None:
        return exit_code

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    reconnect_chatgpt = provider_choice == "chatgpt_oauth"
    plan = await _build_plan(owner_id, roster)
    _print_plan(owner_id, plan, dry_run=dry_run, reconnect_chatgpt=reconnect_chatgpt)
    if dry_run:
        return EXIT_OK

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
    except Exception as exc:  # noqa: BLE001 — classified + released, mirrors provision_runner.run
        released = await _release_hermes_failure_to_error(owner_id, claim_id, claim_id, exc, "migrate prepare")
        return _map_release_exit(released)

    try:
        await provision_hermes_runtime(owner_id, claim_id=claim_id, attempt_token=claim_id, migrate=True)
    except ProvisionSuperseded:
        logger.warning("migrate: lost the provision claim for owner=%s during runtime provision", owner_id)
        return EXIT_LOST_CLAIM
    except Exception as exc:  # noqa: BLE001
        released = await _release_hermes_failure_to_error(
            owner_id, claim_id, claim_id, exc, "migrate runtime provision"
        )
        return _map_release_exit(released)

    # The runtime is READY and the row is terminal (runtime_host=agentcore, status=running) --
    # nothing above this line can clobber the EFS overrides below (see the module docstring's
    # ordering note). A failure from here on is a live-workspace cleanup step, not a lost
    # migration, so it is classified rather than treated as a lost claim.
    try:
        await _apply_efs_overrides(owner_id, plan, provider_choice=provider_choice, byo_provider=byo_provider)
        if reconnect_chatgpt:
            await _reconcile_chatgpt_oauth(owner_id)
        # Order matters (Ruling M6-R1): the ECS service is stopped only now that the AgentCore
        # runtime is confirmed READY, so two runtimes never serve one owner at once.
        await get_ecs_manager().stop_user_service(owner_id, reason="hermes_migration")
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


async def rollback(owner_id: str) -> int:
    """Ruling M6-R3 — undo a migration in progress or already landed: tear down the AgentCore
    side (session stop THEN delete, per Ruling M4-R16 — see ``_stop_runtime_session``), revoke
    the Hermes-minted bearer generation, delete the boot secret, flip the row back onto ECS
    (fenced, same lease mechanic as ``migrate``), and restart the ECS service. S3 profiles are
    left in place — cheap, and the next ``migrate()`` attempt overwrites them anyway.
    """
    row = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    await _teardown_agentcore(owner_id, row)

    claim_id = await container_repo.takeover_provision(owner_id)
    await container_repo.update_container_leased(
        owner_id,
        claim_id,
        {"runtime_host": "ecs", "agentcore_runtime_arn": None, "status": "stopped"},
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
    """
    rows = await container_repo.scan_all()
    results: list[tuple[str, int]] = []
    for row in rows:
        owner_id = row.get("owner_id")
        if not owner_id or owner_id.startswith(PASS_KEY_PREFIX) or owner_id == REFERENCE_ORG_ID:
            continue
        if runtime_transport.host_for(row) == "agentcore":
            continue
        company = await _load_company(owner_id)
        if not company or company.status != "active":
            logger.info("migrate_all: skipping owner=%s — no active Paperclip company", owner_id)
            continue
        code = await migrate(owner_id, dry_run=dry_run)
        if code == EXIT_TRANSIENT:
            logger.warning("migrate_all: owner=%s exited transient — retrying once", owner_id)
            code = await migrate(owner_id, dry_run=dry_run)
        results.append((owner_id, code))

    tag = "DRY RUN " if dry_run else ""
    print(f"{tag}migrate_all summary:")
    for owner_id, code in results:
        print(f"  {owner_id}: exit={code}")
    return max((code for _, code in results), default=EXIT_OK)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--owner-id")
    target.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout)

    if args.rollback:
        if not args.owner_id:
            parser.error("--rollback requires --owner-id")
        return asyncio.run(rollback(args.owner_id))
    if args.all:
        return asyncio.run(migrate_all(dry_run=args.dry_run))
    return asyncio.run(migrate(args.owner_id, dry_run=args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
