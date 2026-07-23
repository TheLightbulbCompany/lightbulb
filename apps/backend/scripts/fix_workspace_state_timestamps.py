"""One-shot healer: rewrite seeded workspace-state.json timestamps to the
JS-canonical form OpenClaw 7.2's boot migration accepts.

Background
----------
Provisioning seeds ``workspace-{uuid}/.openclaw/workspace-state.json`` with
``bootstrapSeededAt`` / ``setupCompletedAt`` written by Python's
``datetime.now(timezone.utc).isoformat()`` — e.g.
``2026-07-23T06:36:32.300037+00:00`` (6-digit microseconds + ``+00:00``).

OpenClaw 7.2 migrates that legacy file into its SQLite state store at boot and
validates each timestamp with JS ``new Date(v).toISOString() === v`` — i.e.
STRICTLY ``YYYY-MM-DDTHH:MM:SS.mmmZ`` (exactly 3 fractional digits + literal
``Z``). Python's ``isoformat()`` fails that round-trip, so the boot doctor logs::

    Failed reading legacy workspace state: legacy workspace setup bootstrap
    timestamp is invalid

the migration aborts, the legacy file survives, and
``assertNoUnmigratedWorkspaceState`` then throws on EVERY agent run::

    Legacy workspace setup state requires migration ... run openclaw doctor --fix.

The forward fix (seed JS-canonical timestamps) ships alongside this script, but
workspaces already staged on EFS before that deploy keep the bad file. This
one-shot rewrites them in place, converting (NOT regenerating) each non-canonical
timestamp so the recorded instant is preserved (truncated to millisecond
precision, matching JS ``Date``).

IMPORTANT — bounce the container after fixing
---------------------------------------------
Rewriting the EFS file does NOT re-run the migration by itself. The boot doctor
only re-reads workspace-state.json when the container (re)starts. After this
script fixes an owner's files, the affected CONTAINER must be bounced (redeploy)
so the boot doctor re-runs the migration against the now-canonical file. This
script does NOT bounce containers — do that separately, e.g.::

    aws ecs update-service --cluster <cluster> --service openclaw-<owner>-<hash> \\
        --force-new-deployment --profile isol8-admin

or via the backend's reprovision/redeploy path.

Usage
-----
Runs where EFS is mounted — i.e. via ECS exec on the backend task (the per-user
EFS filesystem is mounted at ``/mnt/efs/users`` there). Plain filesystem paths;
reads/writes go through the ``Workspace`` abstraction so rewrites keep the
per-user access-point ownership (uid/gid 1000) the container needs.

Dry-run (default) — report what WOULD change, touch nothing::

    cd apps/backend
    uv run python -m scripts.fix_workspace_state_timestamps --env dev \\
        --owner-id user_abc --owner-id org_xyz

Apply::

    uv run python -m scripts.fix_workspace_state_timestamps --env prod \\
        --owner-id user_abc --apply

Fix every container created after the 7.2 promote (born-broken cohort)::

    uv run python -m scripts.fix_workspace_state_timestamps --env prod \\
        --all-since 2026-07-22T00:00:00+00:00 --apply

Idempotent: already-canonical timestamps are left untouched, so re-running is
safe. Archive this script under ``scripts/archive/`` once the cohort is healed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Timestamp fields in workspace-state.json that OpenClaw 7.2's migration
# validates. version is an int; only these two are timestamps.
_TS_FIELDS = ("bootstrapSeededAt", "setupCompletedAt")

# JS-canonical shape: exactly 3 fractional digits + literal Z. Anything else is
# rewritten; a value already matching this is left as-is (idempotent).
_CANONICAL_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

_STATE_REL = "workspace-{agent_id}/.openclaw/workspace-state.json"


def _to_canonical(value: str) -> str | None:
    """Convert an ISO-8601 timestamp to JS-canonical ``...mmmZ`` form, preserving
    the original instant (truncated to millisecond precision). Returns None if
    the value can't be parsed as a timestamp.

    ``datetime.fromisoformat`` (3.11+) parses both the ``+00:00`` offset our
    seeder wrote and a trailing ``Z``; truncating with ``microsecond // 1000``
    matches JS ``Date`` precision and the forward-fix ``js_iso_now`` helper.
    """
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _fix_state(data: dict) -> tuple[dict, list[str]]:
    """Return (possibly-updated copy, names of fields rewritten).

    Only non-canonical string timestamps are touched; the dict's key order and
    every other field are preserved so the rewritten JSON matches the seeder's
    shape byte-for-byte apart from the two timestamps.
    """
    out = dict(data)
    changed: list[str] = []
    for field_name in _TS_FIELDS:
        value = out.get(field_name)
        if not isinstance(value, str) or _CANONICAL_RE.match(value):
            continue  # absent, non-string, or already canonical
        canonical = _to_canonical(value)
        if canonical and canonical != value:
            out[field_name] = canonical
            changed.append(field_name)
    return out, changed


@dataclass
class OwnerSummary:
    owner_id: str
    found: int = 0
    fixed: int = 0
    skipped: int = 0  # present + already-canonical (or unparseable)
    errors: list[str] = field(default_factory=list)


def _parse_since(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise SystemExit(f"--all-since is not a valid ISO-8601 timestamp: {raw!r} ({exc})")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _owners_created_since(since: datetime) -> list[str]:
    """Owner ids whose container row was created at/after ``since``.

    Scans the containers table (one-shot; DDB Scan is fine at fleet size).
    ``created_at`` is ``utc_now_iso()`` (Python isoformat) — parse + normalize
    to UTC before comparing.
    """
    from core.repositories import container_repo

    owners: set[str] = set()
    for row in await container_repo.scan_all():
        owner_id = row.get("owner_id")
        created = row.get("created_at")
        if not owner_id or not isinstance(created, str):
            continue
        try:
            created_dt = datetime.fromisoformat(created)
        except ValueError:
            continue
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        if created_dt.astimezone(timezone.utc) >= since:
            owners.add(owner_id)
    return sorted(owners)


def _heal_owner(workspace, owner_id: str, *, apply: bool) -> OwnerSummary:
    """Scan + (optionally) rewrite every workspace-state.json for one owner."""
    from core.containers.workspace import WorkspaceError

    summary = OwnerSummary(owner_id=owner_id)
    try:
        agent_ids = workspace.list_workspace_agent_dirs(owner_id)
    except WorkspaceError as exc:
        summary.errors.append(f"list dirs failed: {exc}")
        return summary

    for agent_id in agent_ids:
        rel = _STATE_REL.format(agent_id=agent_id)
        try:
            raw = workspace.read_file(owner_id, rel)
        except WorkspaceError:
            continue  # this agent has no state file — nothing to heal
        summary.found += 1

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            summary.skipped += 1
            summary.errors.append(f"{rel}: unparseable JSON ({exc})")
            continue

        updated, changed = _fix_state(data)
        if not changed:
            summary.skipped += 1
            continue

        if apply:
            try:
                # write_file preserves the access-point ownership (uid/gid 1000)
                # so the container can still read the file after the rewrite.
                workspace.write_file(owner_id, rel, json.dumps(updated))
            except WorkspaceError as exc:
                summary.errors.append(f"{rel}: write failed ({exc})")
                continue
        summary.fixed += 1
        print(f"    {'FIXED ' if apply else 'WOULD FIX'} {rel}: {', '.join(changed)}")

    return summary


async def main(*, env: str, owner_ids: list[str], all_since: str | None, apply: bool) -> int:
    from core.config import settings
    from core.containers.workspace import Workspace

    mount = os.environ.get("EFS_MOUNT_PATH") or settings.EFS_MOUNT_PATH or "/mnt/efs/users"
    workspace = Workspace(mount_path=mount)

    if all_since:
        owners = await _owners_created_since(_parse_since(all_since))
    else:
        owners = sorted(dict.fromkeys(owner_ids))

    mode = "APPLY" if apply else "DRY-RUN"
    print(f"[{env}] {mode} — EFS mount {mount} — {len(owners)} owner(s)")
    if all_since:
        print(f"[{env}] selected via --all-since {all_since}")
    if not owners:
        print("No owners to process.")
        return 0

    totals = OwnerSummary(owner_id="(all)")
    for owner_id in owners:
        summary = _heal_owner(workspace, owner_id, apply=apply)
        totals.found += summary.found
        totals.fixed += summary.fixed
        totals.skipped += summary.skipped
        totals.errors.extend(summary.errors)
        print(
            f"  {owner_id}: found={summary.found} "
            f"{'fixed' if apply else 'would-fix'}={summary.fixed} "
            f"skipped={summary.skipped}" + (f" errors={len(summary.errors)}" if summary.errors else "")
        )
        for err in summary.errors:
            print(f"      ERR {err}")

    print(
        f"[{env}] TOTAL found={totals.found} "
        f"{'fixed' if apply else 'would-fix'}={totals.fixed} "
        f"skipped={totals.skipped} errors={len(totals.errors)}"
    )
    if not apply and totals.fixed:
        print("DRY RUN — pass --apply to rewrite the files above.")
    if apply and totals.fixed:
        print(
            "REMINDER: bounce each fixed owner's container (force-new-deployment) "
            "so the boot doctor re-runs the migration against the canonical file."
        )
    return 1 if totals.errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Rewrite seeded workspace-state.json timestamps to the JS-canonical form "
            "OpenClaw 7.2's boot migration accepts. Defaults to dry-run."
        ),
    )
    parser.add_argument("--env", required=True, choices=["dev", "prod"], help="Environment label (informational).")
    parser.add_argument(
        "--owner-id",
        action="append",
        default=[],
        dest="owner_ids",
        help="Owner id to heal (repeatable). Mutually exclusive with --all-since.",
    )
    parser.add_argument(
        "--all-since",
        default=None,
        help="Heal every owner whose container was created at/after this ISO-8601 timestamp.",
    )
    parser.add_argument("--apply", action="store_true", help="Rewrite files (default: dry-run).")
    args = parser.parse_args()

    if bool(args.owner_ids) == bool(args.all_since):
        parser.error("provide exactly one of --owner-id (repeatable) or --all-since")

    sys.exit(
        asyncio.run(
            main(
                env=args.env,
                owner_ids=args.owner_ids,
                all_since=args.all_since,
                apply=args.apply,
            )
        )
    )
