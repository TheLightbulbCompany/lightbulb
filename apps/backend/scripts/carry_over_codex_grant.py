"""Carry a migrated owner's LIVE ChatGPT (Codex) grant from their retired ECS container's
litestream replica into their Hermes root ``auth.json`` — instead of revoking it and making them
reconnect.

**Why this exists.** ``migrate_workspace_to_hermes`` revokes a ChatGPT owner's DDB grant
(Ruling M6-R12) and the owner reconnects on their next visit. The ruling's premise is correct —
the DDB copy is a connect-time SEED and the container's own OpenAI client rotated the single-use
refresh token in place on first use, so staging that copy risks OpenAI revoking the whole token
family. But the founder migration measured the cost: **~18 minutes of hard-failing turns**
(``Provider 'openai-codex' is set in config.yaml but no API key was found``) until the owner
noticed and reconnected. For a customer that window is the whole risk.

The ROTATED grant is not lost — it lives in the container's OpenClaw state DB
(9.3+ keeps the shared auth store in ``state/openclaw.sqlite`` → ``config_machine_state`` row
``authProfiles.store``), which is on the task-local volume and whose only durable copy is the
litestream replica at ``s3://<OWNER_STATE_BUCKET>/<owner>/state/openclaw.sqlite/``. Hermes needs
only ``{access_token, refresh_token}`` (``hermes_cli/auth_codex.py``), and
``hermes_profiles.stage_codex_auth`` already writes exactly that into the root ``auth.json``
under an ETag-fenced read-modify-write.

**Ordering is what makes it safe, and it is the OPERATOR's job.** The extracted refresh token is
unspent only if the container can no longer rotate it, so this runs AFTER the migration's
``stop_user_service(force=True)``. Per owner:

    # 1. migrate, keeping the DDB grant instead of revoking it
    uv run python -m scripts.migrate_workspace_to_hermes --owner-id <org> --keep-chatgpt-grant
    # 2. prove the replica holds a usable grant (stages nothing)
    uv run python -m scripts.carry_over_codex_grant --owner-id <org> --dry-run
    # 3. stage it
    uv run python -m scripts.carry_over_codex_grant --owner-id <org>
    # 4. take a turn in the workspace

If step 2 or 3 fails, fall back to the default migration behaviour — revoke the DDB grant
(``--owner-id <org>`` with no ``--keep-chatgpt-grant``, or ``oauth_service.revoke_user_oauth``)
so ``provision_gate``'s ``oauth_required`` check fires and the owner reconnects cleanly.

**This one runs from an OPERATOR LAPTOP, not inside the backend task** — unlike
``migrate_workspace_to_hermes`` / ``roll_hermes_runtimes``, it needs no EFS mount, and it needs
the ``litestream`` binary, which is in the OpenClaw image but not the backend one. It needs:

  * ``litestream`` 0.5.16 on ``PATH`` (``brew install benbjohnson/litestream/litestream``)
  * admin AWS credentials — ``AWS_PROFILE=isol8-admin AWS_REGION=us-east-1``, used by both the
    litestream restore and the boto3 ``stage_codex_auth`` write
  * ``OWNER_STATE_BUCKET=lightbulb-prod-openclaw-replicas`` (``lightbulb-{env}-openclaw-replicas``
    — ONE bucket holds both the litestream replicas and the Hermes profiles)
  * an ``apps/backend/.env`` (any ``CLERK_ISSUER``; ``core.config.Settings`` refuses to construct
    without one). Nothing here reads DynamoDB, so no table prefix / ``ENVIRONMENT`` is needed.

    cd apps/backend && AWS_PROFILE=isol8-admin AWS_REGION=us-east-1 \\
      OWNER_STATE_BUCKET=lightbulb-prod-openclaw-replicas \\
      uv run python -m scripts.carry_over_codex_grant --owner-id <org> [--dry-run]

Token VALUES are never printed or logged — ``--dry-run`` reports only the profile key, the
account email, whether each token is present, and the expiry.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

#: The state DB's replica prefix, and the table/row the shared auth store lives in on
#: OpenClaw >= 9.3 (see ``core.containers.workspace``'s own 2026-09-15 note and the boot script's
#: sentinel step, which clears this exact row).
_REPLICA_SUFFIX = "state/openclaw.sqlite"
_STORE_ROW = "authProfiles.store"
#: The profile key OpenClaw writes a ChatGPT login under. The fallback below covers a store whose
#: key differs (a renamed profile) but that still holds exactly one OpenAI grant.
_PROFILE_KEY = "openai:default"
_RESTORE_TIMEOUT_S = 120


class CarryOverError(RuntimeError):
    """Anything that makes the carry-over impossible. The message is the operator's reason and
    never contains a token."""


def _restore(litestream: str, replica_url: str, dest: Path) -> None:
    """``litestream restore -o <dest> <replica-url>``. No ``-if-replica-exists``: a missing
    replica must fail loudly here, not silently produce no grant."""
    try:
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [litestream, "restore", "-o", str(dest), replica_url],
            capture_output=True,
            text=True,
            timeout=_RESTORE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise CarryOverError(f"litestream restore timed out after {_RESTORE_TIMEOUT_S}s") from exc
    if result.returncode != 0:
        raise CarryOverError(f"litestream restore exited {result.returncode}: {result.stderr.strip()[:500]}")
    if not dest.is_file():
        raise CarryOverError(f"litestream restore reported success but wrote no file at {dest}")


def _read_profile(db_path: Path) -> tuple[str, dict]:
    """``(profile_key, profile)`` for the owner's OpenAI grant, read-only."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise CarryOverError(f"could not open the restored state DB: {exc}") from exc
    try:
        row = conn.execute("SELECT value_json FROM config_machine_state WHERE state_key = ?", (_STORE_ROW,)).fetchone()
    except sqlite3.Error as exc:
        raise CarryOverError(f"could not read {_STORE_ROW} from the restored state DB: {exc}") from exc
    finally:
        conn.close()
    if not row or not row[0]:
        raise CarryOverError(f"the restored state DB has no {_STORE_ROW} row — the container never held a grant")
    try:
        profiles = (json.loads(row[0]) or {}).get("profiles") or {}
    except json.JSONDecodeError as exc:
        raise CarryOverError(f"{_STORE_ROW} is not valid JSON: {exc}") from exc
    if _PROFILE_KEY in profiles:
        return _PROFILE_KEY, profiles[_PROFILE_KEY]
    openai = [(k, v) for k, v in profiles.items() if isinstance(v, dict) and v.get("provider") == "openai"]
    if len(openai) != 1:
        raise CarryOverError(
            f"expected one OpenAI profile in {_STORE_ROW}, found {len(openai)} (keys: {sorted(profiles)})"
        )
    return openai[0]


def _summary(profile_key: str, profile: dict) -> dict:
    """What ``--dry-run`` prints. Presence booleans, never the values."""
    return {
        "profile_key": profile_key,
        "email": profile.get("email"),
        "has_access": bool(profile.get("access")),
        "has_refresh": bool(profile.get("refresh")),
        "expires_at": profile.get("expires"),
    }


def carry_over(owner_id: str, *, dry_run: bool) -> dict:
    """Restore, extract and (unless ``dry_run``) stage. Returns the printable summary."""
    from core.config import settings
    from core.services.hermes_profiles import stage_codex_auth

    litestream = shutil.which("litestream")
    if not litestream:
        raise CarryOverError("litestream is not on PATH (brew install benbjohnson/litestream/litestream)")
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        raise CarryOverError("OWNER_STATE_BUCKET is not set (lightbulb-{env}-openclaw-replicas)")

    with tempfile.TemporaryDirectory(prefix="codex-carryover-") as tmp:
        db_path = Path(tmp) / "openclaw.sqlite"
        _restore(litestream, f"s3://{bucket}/{owner_id}/{_REPLICA_SUFFIX}", db_path)
        profile_key, profile = _read_profile(db_path)
        summary = _summary(profile_key, profile)
        if not (summary["has_access"] and summary["has_refresh"]):
            raise CarryOverError(f"profile {profile_key} is missing an access or refresh token — nothing to carry")
        if not dry_run:
            stage_codex_auth(owner_id, {"access_token": profile["access"], "refresh_token": profile["refresh"]})
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="restore + extract only; stage nothing")
    args = parser.parse_args(argv)

    try:
        summary = carry_over(args.owner_id, dry_run=args.dry_run)
    except CarryOverError as exc:
        logger.error("carry_over: owner=%s FAILED: %s", args.owner_id, exc)
        return 1
    print(json.dumps(summary, sort_keys=True))
    if args.dry_run:
        print(f"DRY RUN: staged nothing for {args.owner_id}")
    else:
        print(f"staged the ChatGPT grant into {args.owner_id}'s root auth.json")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    sys.exit(main())
