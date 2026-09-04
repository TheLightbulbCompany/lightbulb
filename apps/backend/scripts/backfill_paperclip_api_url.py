"""One-shot ops CLI: point every per-agent ``paperclip-claimed-api-key.json``
on EFS at the CURRENT Paperclip host.

Why this exists: the 2026-08-28 Cloud Map rename (#2115, old namespace deleted
in #2132, both promoted to prod 2026-08-30) moved Paperclip from
``paperclip.isol8-{env}.local`` to ``paperclip.lightbulb-{env}.local``. The
backend's ``PAPERCLIP_INTERNAL_URL`` followed automatically and the
Paperclip-side ``adapterConfig.paperclipApiUrl`` was reconciled, but the
per-agent identity file the ``paperclip-exec-creds`` plugin turns into
``PAPERCLIP_API_URL`` on every exec is write-if-missing and was never
touched — so every container provisioned before the promotion still names the
dead host and every webchat Paperclip call fails DNS. Provisioning now
refreshes the field (``friday_identity._refresh_stale_api_url``); this script
is the backfill for the containers nobody will reprovision.

Only the ``apiUrl`` field is rewritten; the agent's own ``token`` and the rest
of the document are preserved byte-for-byte as JSON. Files that do not parse
are reported and left alone.

Run INSIDE the deployed backend task (EFS is mounted there, ``ENVIRONMENT`` and
``PAPERCLIP_INTERNAL_URL`` come from CDK)::

    aws ecs execute-command --cluster lightbulb-{env}-container \\
        --task <backend task id> --container backend --interactive \\
        --command "uv run python -m scripts.backfill_paperclip_api_url"          # dry run
    aws ecs execute-command ... \\
        --command "uv run python -m scripts.backfill_paperclip_api_url --apply"  # write

Idempotent: a second run finds nothing stale. DELETE this script (and its test)
once both environments are backfilled — CLAUDE.md, "Planning docs are
disposable": git history is the run record.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Mirrors core/services/friday_identity.py and the paperclip-exec-creds
# plugin (apps/infra/openclaw/plugins/paperclip-exec-creds/dist/resolve.js).
KEY_FILENAME = "paperclip-claimed-api-key.json"


@dataclass
class Summary:
    stale: list[Path] = field(default_factory=list)
    rewritten: list[Path] = field(default_factory=list)
    skipped: list[tuple[Path, str]] = field(default_factory=list)
    current: int = 0


def backfill(root: Path, target_url: str, *, apply: bool) -> Summary:
    """Walk ``<root>/<owner>/workspace-<agent>/<KEY_FILENAME>`` and rewrite the
    ``apiUrl`` of every file that does not already equal ``target_url``.

    Writes in place (truncate + write) so the file keeps its inode owner/mode —
    the node user only needs to READ it. ``apply=False`` reports without
    writing.
    """
    summary = Summary()
    for path in sorted(root.glob(f"*/workspace-*/{KEY_FILENAME}")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            summary.skipped.append((path, f"unreadable: {exc}"))
            continue
        if not isinstance(doc, dict):
            summary.skipped.append((path, "not a JSON object"))
            continue
        if doc.get("apiUrl") == target_url:
            summary.current += 1
            continue
        summary.stale.append(path)
        if not apply:
            continue
        try:
            path.write_text(json.dumps({**doc, "apiUrl": target_url}), encoding="utf-8")
        except OSError as exc:
            summary.skipped.append((path, f"write failed: {exc}"))
            continue
        summary.rewritten.append(path)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    parser.add_argument("--root", help="override the EFS users root (default: settings.EFS_MOUNT_PATH)")
    parser.add_argument("--target-url", help="override the target (default: settings.PAPERCLIP_INTERNAL_URL)")
    args = parser.parse_args(argv)

    # Lazy import so --help works without the backend env.
    from core.config import settings

    root = Path(args.root or settings.EFS_MOUNT_PATH)
    target = args.target_url or settings.PAPERCLIP_INTERNAL_URL
    if not target:
        print("PAPERCLIP_INTERNAL_URL is empty — refusing to write an empty apiUrl", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"EFS root {root} is not a directory — run this inside the backend task", file=sys.stderr)
        return 2

    summary = backfill(root, target, apply=args.apply)
    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"[{mode}] root={root} target={target}")
    print(f"  current : {summary.current}")
    print(f"  stale   : {len(summary.stale)}")
    for p in summary.stale:
        print(f"    {'rewrote' if p in summary.rewritten else 'would rewrite'} {p.relative_to(root)}")
    for p, why in summary.skipped:
        print(f"  skipped {p.relative_to(root)}: {why}")
    if not args.apply and summary.stale:
        print("pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
