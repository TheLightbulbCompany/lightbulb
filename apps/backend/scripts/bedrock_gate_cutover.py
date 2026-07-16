"""Cutover tooling: flip the per-owner ``bedrock_via_gateway`` flag and
reprovision so Bedrock traffic starts (or stops) routing through the
bedrock-gate Lambda.

The bedrock-gate rollout is controlled entirely by a single containers-row
attribute — ``bedrock_via_gateway`` (BOOL, default absent/false). When it is
true, the config generator points the ``amazon-bedrock`` provider ``baseUrl``
at the gateway Function URL and the task-def injects the per-owner bearer
token env (``AWS_BEARER_TOKEN_BEDROCK`` + friends); when it is false/absent,
the container talks to Bedrock directly exactly as it does today. See
``docs/superpowers/specs/2026-07-15-bedrock-gate-design.md`` §5.5 and §9.

This script does NOT invent a provision path. It:

  1. sets ``bedrock_via_gateway`` on the containers row via
     :func:`core.repositories.container_repo.update_fields`, then
  2. triggers a full reprovision via
     :meth:`core.containers.ecs_manager.EcsManager.reprovision_for_user` —
     the SAME admin-dashboard path that re-registers the task definition
     (picking up the new bearer-token env) AND rewrites ``openclaw.json`` on
     EFS from the owner's current settings (picking up the new ``baseUrl``),
     then rolls the service. A flag flip therefore lands both halves in one
     call. The break-glass runbook
     (``docs/runbooks/bedrock-gate-break-glass.md``) documents the faster
     ``config_patcher.replace_provider_block`` hot-swap for emergency reverts.

**Only Bedrock-path owners are ever touched.** BYOK (``byo_key``) and
ChatGPT-OAuth (``chatgpt_oauth``) owners have no Bedrock in their inference
path, so the flag is meaningless for them — they are excluded from cohort
resolution and refused in single-owner mode. An owner is Bedrock-path when
either:

  * ``subscription_status == "trial_local"``  → the ``trial`` cohort (no-card
    free tier, pinned to the single Sonnet AIP), or
  * ``provider_choice == "bedrock_claude"``   → the ``paid`` cohort
    (Isol8-billed Bedrock credits).

Run via::

    cd apps/backend
    # single owner, dry-run then apply
    uv run python -m scripts.bedrock_gate_cutover --owner org_xxx --env prod
    uv run python -m scripts.bedrock_gate_cutover --owner org_xxx --env prod --apply

    # whole cohort, dry-run then apply
    uv run python -m scripts.bedrock_gate_cutover --cohort trial --env dev
    uv run python -m scripts.bedrock_gate_cutover --cohort trial --env dev --apply

    # emergency revert (see the break-glass runbook)
    uv run python -m scripts.bedrock_gate_cutover --cohort trial --env prod --rollback --apply

Defaults to dry-run; ``--apply`` is required to mutate anything. Idempotent:
setting the flag to a value it already holds + reprovisioning is safe (a
reprovision preserves EFS data, the gateway token, and the EFS access point).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("bedrock_gate_cutover")

# Container-row attribute that gates every gateway behavior change. Kept as a
# module constant so the flag name lives in exactly one place across the
# script + tests (the config generator / task-def emitter read the same key).
FLAG_ATTR = "bedrock_via_gateway"


def _classify_plan(billing: dict | None) -> str | None:
    """Return the Bedrock cohort an owner belongs to, or ``None``.

    ``"trial"``  — card-less free tier (``subscription_status == "trial_local"``).
    ``"paid"``   — Isol8-billed Bedrock (``provider_choice == "bedrock_claude"``).
    ``None``     — not a Bedrock-path owner (BYOK / ChatGPT-OAuth / no billing
                   row). These are NEVER touched by the cutover.

    Trial is checked first: a ``trial_local`` row carries no
    ``provider_choice`` (``bedrock_trial`` is a config-path mode, never a
    stored choice), so the order only matters as documentation of intent.
    """
    if not billing:
        return None
    if billing.get("subscription_status") == "trial_local":
        return "trial"
    if billing.get("provider_choice") == "bedrock_claude":
        return "paid"
    return None


async def _resolve_targets(
    *,
    owner: str | None,
    cohort: str | None,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Resolve the owners in scope to ``[(owner_id, plan)]`` plus a list of
    human-readable SKIP notes for owners that were considered but excluded.

    Single-owner mode requires the container row to exist and the owner to be
    Bedrock-path. Cohort mode scans the containers table and keeps only owners
    whose plan matches ``cohort``.
    """
    from core.repositories import billing_repo, container_repo

    targets: list[tuple[str, str]] = []
    skips: list[str] = []

    if owner is not None:
        container = await container_repo.get_by_owner_id(owner)
        if not container:
            skips.append(f"{owner}: no container row")
            return targets, skips
        billing = await billing_repo.get_by_owner_id(owner)
        plan = _classify_plan(billing)
        if plan is None:
            skips.append(f"{owner}: not a Bedrock-path owner (BYOK / OAuth / no billing) — refusing")
            return targets, skips
        targets.append((owner, plan))
        return targets, skips

    # Cohort mode: scan every container row, classify by billing, keep matches.
    rows = await container_repo.scan_all()
    seen: set[str] = set()
    for row in rows:
        owner_id = row.get("owner_id")
        if not owner_id or owner_id in seen:
            continue
        seen.add(owner_id)
        billing = await billing_repo.get_by_owner_id(owner_id)
        plan = _classify_plan(billing)
        if plan is None:
            continue  # BYOK / OAuth / no billing — silently out of scope
        if plan != cohort:
            continue  # other Bedrock cohort — not this run's target
        targets.append((owner_id, plan))
    return sorted(targets), skips


async def main(
    *,
    apply: bool,
    env: str,
    owner: str | None = None,
    cohort: str | None = None,
    rollback: bool = False,
) -> int:
    # The reprovision path re-renders openclaw.json via the config generator,
    # which stamps version-gated keys off ENVIRONMENT (deployed image tag).
    # This CLI runs outside the deployed backend, so set it from --env — same
    # reasoning as reapply_openclaw_config. Does NOT change DDB/EFS targeting
    # (that is keyed on DYNAMODB_TABLE_PREFIX).
    os.environ["ENVIRONMENT"] = env

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Imports inside main so script setup (env vars) happens first — matches
    # the backfill / reapply scripts' lazy-import convention.
    from core.containers import get_ecs_manager
    from core.repositories import container_repo

    if (owner is None) == (cohort is None):
        print("ERROR: pass exactly one of --owner or --cohort")
        return 2

    desired = not rollback  # cutover -> True; rollback -> False
    action = "rollback (set false)" if rollback else "cutover (set true)"
    scope = f"owner={owner}" if owner is not None else f"cohort={cohort}"

    print(f"[{env}] bedrock-gate {action} | {scope} | apply={apply}")

    targets, skips = await _resolve_targets(owner=owner, cohort=cohort)

    for note in skips:
        print(f"  SKIP {note}")

    if not targets:
        print("No Bedrock-path owners in scope — nothing to do.")
        # A refused single owner is an operator error worth a non-zero exit;
        # an empty cohort is a legitimate no-op.
        return 1 if (owner is not None and skips) else 0

    trial_n = sum(1 for _, plan in targets if plan == "trial")
    paid_n = sum(1 for _, plan in targets if plan == "paid")
    print(f"Resolved {len(targets)} target owner(s):")
    for owner_id, plan in targets:
        print(f"  PLAN owner={owner_id} plan={plan} set {FLAG_ATTR}={desired} + reprovision")
    print(f"Counts: trial={trial_n} paid={paid_n} total={len(targets)}")

    # A FORWARD cutover is inert unless the backend actually knows the gate URL:
    # config.py keeps the direct bedrock-runtime baseUrl and ecs_manager skips
    # the bearer env whenever BEDROCK_GATE_URL is empty (default). Setting the
    # flag + reprovisioning would then print OK while leaving traffic on direct
    # Bedrock — a silent non-cutover. Refuse rather than mislead. (Rollback does
    # not need the URL — it reverts to direct on purpose.)
    from core.config import settings

    if desired and not settings.BEDROCK_GATE_URL:
        print(
            "REFUSING: BEDROCK_GATE_URL is empty in this backend env, so a "
            "cutover would silently leave traffic on direct Bedrock. Wire the "
            "bedrock-gate Function URL into the backend env first (see "
            "docs/runbooks/bedrock-gate-break-glass.md)."
        )
        return 2

    if not apply:
        print("DRY RUN — pass --apply to set the flag and reprovision")
        return 0

    ecs_manager = get_ecs_manager()
    failures: list[str] = []
    for owner_id, plan in targets:
        try:
            # Order matters: the reprovision reads bedrock_via_gateway off the
            # containers row when it re-renders openclaw.json + the task-def
            # env, so the flag MUST be persisted before we reprovision.
            await container_repo.update_fields(owner_id, {FLAG_ATTR: desired})
            await ecs_manager.reprovision_for_user(owner_id)
            print(f"  OK   {owner_id}: {FLAG_ATTR}={desired} + reprovision dispatched")
        except Exception as e:  # noqa: BLE001
            print(f"  ERR  {owner_id}: {e}")
            failures.append(owner_id)

    if failures:
        print(f"FAILED for {len(failures)} owner(s): {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Flip the per-owner bedrock_via_gateway cutover flag and "
            "reprovision so Bedrock traffic routes through (or stops routing "
            "through) the bedrock-gate Lambda. Only Bedrock-path owners "
            "(trial_local / bedrock_claude) are touched. Defaults to dry-run."
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--owner",
        help="Single container owner id (org_* for orgs, user_* for personal owners).",
    )
    group.add_argument(
        "--cohort",
        choices=["trial", "paid"],
        help="Target every Bedrock-path owner of this plan.",
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help=(
            "Target environment. Sets ENVIRONMENT so the reprovision's config "
            "regen resolves THIS env's deployed image tag. Does NOT change "
            "DDB/EFS targeting (keyed on DYNAMODB_TABLE_PREFIX)."
        ),
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Reverse the cutover: set bedrock_via_gateway=false (default sets true).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually set the flag + reprovision (default: dry-run).",
    )
    args = parser.parse_args()
    sys.exit(
        asyncio.run(
            main(
                apply=args.apply,
                env=args.env,
                owner=args.owner,
                cohort=args.cohort,
                rollback=args.rollback,
            )
        )
    )
