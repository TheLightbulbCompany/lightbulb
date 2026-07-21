"""Force-restage Atlas's workspace bundle onto an existing container's EFS.

Atlas's seed files (``core/services/atlas_seed/files/``) are staged at
provision time by ``pre_stage_atlas`` via ``write_file_if_missing`` (O_EXCL):
it writes a file ONLY if it does not already exist. So a container that was
provisioned before the bundle was rewritten keeps the OLD files on a normal
reprovision — the new (e.g. gateway-free) workflow bundle is never picked up.

This script calls ``pre_stage_atlas(..., force=True)``, which OVERWRITES every
staged file via ``Workspace.write_file`` instead, bringing an existing
container current with the repo bundle.

Scope — what this touches and what it does NOT:

* Touches ONLY Atlas's workspace files on EFS
  (``users/<owner>/workspace-<atlas_uuid>/...``). The bridge-URL
  substitution + ACP augmentation are applied exactly as a fresh provision
  would produce them.
* Does NOT touch Friday or any other agent's workspace, openclaw.json, the
  ECS service/container, DynamoDB rows, Stripe/billing, or any Paperclip
  record. It only reads the owner's Paperclip agent list (to find Atlas's
  UUID) and writes files under Atlas's workspace dir.

Run from ``apps/backend`` (so ``core.*`` is importable and settings reflect
the target environment — e.g. inside the backend ECS task for prod)::

    cd apps/backend
    # DRY RUN — show what WOULD be overwritten, write nothing:
    uv run python -m scripts.restage_atlas --env prod --owner-id org_xxx
    # APPLY — force-overwrite Atlas's files:
    uv run python -m scripts.restage_atlas --env prod --owner-id org_xxx --apply

``--owner-id`` may be repeated and/or comma-separated
(``--owner-id a,b --owner-id c``). ``--all-from-containers`` restages every
owner in the ``containers`` table (paginated). Defaults to dry-run; pass
``--apply`` to actually overwrite.

Idempotent: re-running re-overwrites with the same bundle. Owners with no
Paperclip company / no Atlas agent / an ambiguous "Atlas" name are reported
and skipped (``pre_stage_atlas`` refuses to seed in those cases).

``--env`` is informational only (matches ``backfill_paperclip_for_legacy_users``):
DDB/EFS targeting comes from the process's environment + settings, not this flag.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("restage_atlas")


def _split_owner_ids(raw: list[str] | None) -> list[str]:
    """Flatten repeated and/or comma-separated ``--owner-id`` values, preserving
    first-seen order and dropping blanks/dupes."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in raw or []:
        for part in chunk.split(","):
            owner = part.strip()
            if owner and owner not in seen:
                seen.add(owner)
                out.append(owner)
    return out


async def _scan_owner_ids() -> list[str]:
    """Paginate the ``containers`` table and return every ``owner_id``.

    Mirrors ``backfill_paperclip_for_legacy_users._scan_owner_ids``. Sorted for
    deterministic output across runs.
    """
    from core.dynamodb import get_table, run_in_thread

    table = get_table("containers")
    owner_ids: set[str] = set()
    last_evaluated_key: dict | None = None
    while True:
        kwargs: dict[str, Any] = {}
        if last_evaluated_key is not None:
            kwargs["ExclusiveStartKey"] = last_evaluated_key
        resp = await run_in_thread(table.scan, **kwargs)
        for item in resp.get("Items", []):
            owner_id = item.get("owner_id")
            if owner_id:
                owner_ids.add(owner_id)
        last_evaluated_key = resp.get("LastEvaluatedKey")
        if not last_evaluated_key:
            break
    return sorted(owner_ids)


def _bundle_file_count() -> int:
    """Count the staged files Atlas's bundle would write, applying the same
    junk filter (``_SKIP``) the seeder uses. For the dry-run preview only."""
    from core.services.atlas_seed import _FILES_DIR, _SKIP

    n = 0
    for path in _FILES_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(_FILES_DIR)
        if any(part in _SKIP for part in rel.parts):
            continue
        n += 1
    return n


def _resolve_atlas(agents: list[dict]) -> tuple[str | None, str | None]:
    """Resolve Atlas's UUID from a Paperclip agent list, mirroring
    ``pre_stage_atlas``'s matching. Returns ``(atlas_uuid, reason)`` where
    exactly one is set: a UUID on success, or a skip reason otherwise."""
    from core.services.atlas_seed import _ATLAS_AGENT_NAME

    matches = [a for a in agents if a.get("name") == _ATLAS_AGENT_NAME and a.get("id")]
    if len(matches) > 1:
        return None, "ambiguous"
    if not matches:
        return None, ("no_agents" if not agents else "no_atlas")
    return matches[0]["id"], None


async def main(*, apply: bool, env: str, owner_ids: list[str], all_from_containers: bool) -> int:
    # Imports inside main so script setup (argparse, env) runs first and the
    # module stays import-safe (only stdlib at module load).
    import httpx

    from core.config import settings
    from core.containers import get_workspace
    from core.repositories.paperclip_repo import PaperclipRepo
    from core.services.atlas_seed import pre_stage_atlas
    from core.services.paperclip_openclaw_pairing import fetch_paperclip_agents_for_owner

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if all_from_containers:
        owner_ids = await _scan_owner_ids()

    if not owner_ids:
        print("Nothing to do: pass --owner-id <id> (repeatable/comma-list) or --all-from-containers")
        return 2

    bundle_n = _bundle_file_count()
    print(f"[{env}] Restaging Atlas for {len(owner_ids)} owner(s) — {'APPLY' if apply else 'DRY RUN'}")
    print(f"[{env}] Atlas bundle = {bundle_n} file(s) + 1 workspace-state file")

    workspace = get_workspace()
    # One httpx client + admin client for the whole run, matching the
    # construction pattern in ecs_manager.write_user_configs.
    http = httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0)

    n_ok = 0
    n_skip = 0
    n_err = 0
    total_overwritten = 0
    try:
        from core.services.paperclip_admin_client import PaperclipAdminClient

        repo = PaperclipRepo(table_name="paperclip-companies")
        admin_client = PaperclipAdminClient(http_client=http)

        for owner_id in owner_ids:
            try:
                # Read-only: list the owner's Paperclip agents to find Atlas.
                agents = await fetch_paperclip_agents_for_owner(
                    owner_id=owner_id,
                    repo=repo,
                    admin_client=admin_client,
                )
            except Exception as e:  # noqa: BLE001 — report + continue to next owner
                n_err += 1
                print(f"  ERR  {owner_id}: fetch agents failed: {e}")
                continue

            if not apply:
                atlas_uuid, reason = _resolve_atlas(agents)
                if atlas_uuid is None:
                    n_skip += 1
                    print(f"  SKIP {owner_id}: {reason}")
                    continue
                n_ok += 1
                # Estimate: the vendored bundle file count (bundle_n) + the
                # workspace-state.json guard file. Approximate (~) because the
                # real overwrite count under --apply also reflects the ACP append
                # / bridge-URL substitution writing the same paths once each — it
                # won't exceed this, and is exact for the common no-ACP case.
                would = bundle_n + 1
                total_overwritten += would
                print(f"  WOULD {owner_id}: atlas={atlas_uuid} would overwrite ~{would} file(s)")
                continue

            try:
                result = await pre_stage_atlas(
                    owner_id=owner_id,
                    paperclip_agents=agents,
                    workspace=workspace,
                    force=True,
                )
            except Exception as e:  # noqa: BLE001 — pre_stage_atlas is best-effort, but guard anyway
                n_err += 1
                print(f"  ERR  {owner_id}: restage failed: {e}")
                continue

            if result.get("reason"):
                n_skip += 1
                print(f"  SKIP {owner_id}: {result['reason']}")
                continue
            n_ok += 1
            total_overwritten += result.get("overwritten", 0)
            print(
                f"  OK   {owner_id}: atlas={result.get('atlas_uuid')} "
                f"overwrote={result.get('overwritten', 0)} errored={result.get('errored', 0)}"
            )
    finally:
        await http.aclose()

    verb = "would overwrite" if not apply else "overwrote"
    print(f"[{env}] Done: ok={n_ok} skipped={n_skip} errored={n_err} — {verb} {total_overwritten} file(s) total")
    if not apply:
        print("DRY RUN — pass --apply to overwrite")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Force-overwrite Atlas's EFS workspace bundle for an existing container "
            "(normal reprovision's write_file_if_missing skips existing files). Defaults to dry-run."
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        choices=["dev", "staging", "prod", "local"],
        help="Environment label (informational; targeting comes from process env/settings).",
    )
    parser.add_argument(
        "--owner-id",
        action="append",
        default=[],
        help="Owner id to restage (org_* or user_*). Repeatable and/or comma-separated.",
    )
    parser.add_argument(
        "--all-from-containers",
        action="store_true",
        help="Restage every owner in the containers table (paginated). Overrides --owner-id.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually force-overwrite (default: dry-run).",
    )
    args = parser.parse_args()

    owners = _split_owner_ids(args.owner_id)
    if not owners and not args.all_from_containers:
        parser.error("provide --owner-id (repeatable/comma-list) or --all-from-containers")

    sys.exit(
        asyncio.run(
            main(
                apply=args.apply,
                env=args.env,
                owner_ids=owners,
                all_from_containers=args.all_from_containers,
            )
        )
    )
