"""One-shot ops CLI: regenerate an owner's ``openclaw.json`` on EFS from the
current config generator.

Thin wrapper around ``core.services.update_service.reapply_owner_config`` —
the same function behind the admin dashboard's "Regenerate config" lever
(``POST /admin/users/{user_id}/container/reapply-config``). All regen +
runtime-subtree-preservation logic lives in the service; this CLI only adds
the dry-run gate and the ``--env`` plumbing needed when running OUTSIDE the
deployed backend. See the service docstring for the full semantics
(provisioning write path, runtime-only deep-diff restore, gateway_token
refusal on legacy rows).

Run via::

    cd apps/backend
    uv run python -m scripts.reapply_openclaw_config \\
        --env prod --owner-id org_xxx
    uv run python -m scripts.reapply_openclaw_config \\
        --env prod --owner-id org_xxx --apply

Defaults to dry-run; pass ``--apply`` to actually write. The dry run resolves
everything (the owner's ``provider_choice``, tokens off the containers row,
the target EFS path) and logs exactly what WOULD be written, but does not
call the write path.

Idempotent + safe to re-run: the write path reuses the existing gateway
token, EFS access point, device identities, and agent workspaces; it only
overwrites the config + paired-device files.

Note on rollout: re-writing ``openclaw.json`` lands the new config on EFS,
but a RUNNING container only picks it up via OpenClaw's file watcher
(``CHOKIDAR_USEPOLLING``) or on its next task restart. This script does not
restart the task.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

logger = logging.getLogger("reapply_openclaw_config")


def _runtime_only_delta(existing: dict | None, fresh: dict | None) -> dict:
    """Lazy delegate to the service implementation.

    Kept as a module-level callable (rather than a top-level import) because
    this CLI must set ENVIRONMENT before anything imports ``core.config``
    settings — see main(). The service module is only imported on first call.
    """
    from core.services.update_service import _runtime_only_delta as impl

    return impl(existing, fresh)


async def main(*, apply: bool, env: str, owner_id: str) -> int:
    # The generator (build_openclaw_config_dict, via the service's
    # write_user_configs call) stamps version-gated config keys using
    # openclaw_version.deployed_upstream_version(), which reads ENVIRONMENT to
    # pick the prod vs dev image tag. This CLI is run outside the deployed
    # backend (where CDK injects ENVIRONMENT), so set it from --env or the gate
    # falls back to dev_tag() and could write a newer-only key onto a prod
    # owner whose container is still on an older image (crash-loop on boot).
    # Does NOT change DDB/EFS targeting — that's keyed on DYNAMODB_TABLE_PREFIX.
    os.environ["ENVIRONMENT"] = env

    # Imports inside main so script setup (path, env vars) is done first —
    # matches the backfill script's lazy-import convention.
    from pathlib import Path

    from core.config import settings
    from core.repositories import container_repo
    from core.services import container_provision, update_service

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        print(f"[{env}] No container row for owner {owner_id} — nothing to reapply")
        return 1

    if not container.get("gateway_token"):
        # Legacy rows pre-dating gateway_token storage. The service refuses
        # these too; surface it before the dry-run/apply gate so a dry run
        # already tells the operator this owner needs a full reprovision.
        print(
            f"[{env}] Container row for {owner_id} has no gateway_token — "
            "refusing to reapply (run a full reprovision instead)"
        )
        return 1

    provider_choice, byo_provider = await container_provision.resolve_provider_choice(owner_id)
    config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"

    print(f"[{env}] Reapply openclaw.json for owner {owner_id}")
    print(f"  provider_choice : {provider_choice}")
    print(f"  byo_provider    : {byo_provider}")
    print(f"  owner_type      : {container.get('owner_type') or 'personal'}")
    print(f"  target EFS path : {config_path}")

    if not apply:
        print("DRY RUN — pass --apply to write the regenerated openclaw.json")
        return 0

    try:
        result = await update_service.reapply_owner_config(owner_id)
        print(f"  OK   wrote {config_path}")
        restored = result["restored_subtrees"]
        if restored:
            print(f"  OK   restored runtime subtrees: {', '.join(restored)}")
        else:
            print("  ..   no runtime-only subtrees dropped by the regen")
    except Exception as e:  # noqa: BLE001
        print(f"  ERR  {owner_id}: {e}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Regenerate an owner's openclaw.json on EFS from the current "
            "config generator via the provisioning write path. Defaults to "
            "dry-run."
        ),
    )
    parser.add_argument(
        "--owner-id",
        required=True,
        help="Container owner id (org_* for orgs, user_* for personal owners).",
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "prod"],
        help="Target environment (sets ENVIRONMENT for version-gated config keys).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the regenerated config (default: dry run).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env, owner_id=args.owner_id)))
