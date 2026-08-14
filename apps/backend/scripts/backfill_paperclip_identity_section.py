"""ONE-SHOT: rewrite the Paperclip-identity section in existing AGENTS.md files.

DELETE THIS SCRIPT once it has been run against dev and prod. Per CLAUDE.md, a
backfill whose result is produced at creation time afterwards does not stay in
the tree — git history is the operational record.

WHY IT EXISTS
-------------
Seeded playbooks used to instruct every Paperclip agent to run
``export PAPERCLIP_API_KEY=$(jq -r .token paperclip-claimed-api-key.json)``
before each API call. ``export`` does not survive between exec calls, so the
model re-materialized the token every turn and printed it into its visible
reasoning — a long-lived key in the owner's transcript. Credentials now arrive
via the ``paperclip-exec-creds`` plugin's ``resolve_exec_env`` hook and the
seed template no longer mentions the file, but files already on EFS keep the
old text (they are seeded write-if-missing). This closes that gap.

It covers EVERY agent, not just the CEO: Atlas and company/marketplace
specialists carry their own independently-authored AGENTS.md and can follow
the old instruction just as easily.

WHY A SCRIPT AND NOT A PERMANENT MIGRATION
------------------------------------------
15 owners at the time of writing (11 prod, 4 dev). Carrying permanent heal
code — with the text-surgery hazards that come with it — to serve a one-time
fleet of that size is the wrong trade.

SAFETY
------
Exact-match only. The known legacy block is replaced with the current template
section; anything that does not match is REPORTED and left untouched for a
human to look at, never regex-surgered. AGENTS.md is a living playbook, so
guessing at section boundaries risks deleting an owner's own writing.

USAGE
-----
    uv run python -m scripts.backfill_paperclip_identity_section            # dry run
    uv run python -m scripts.backfill_paperclip_identity_section --apply    # write

Needs DYNAMODB_TABLE_PREFIX and the EFS mount, i.e. run it inside the backend
task (ECS exec), not from a laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from core.containers.workspace import Workspace
from core.repositories import container_repo
from core.services.friday_identity import _AGENTS_TEMPLATE

logger = logging.getLogger("backfill_paperclip_identity")

_NEW_HEADING = "## Your Paperclip identity — already in your shell"
_LEGACY_HEADING = "## Your Paperclip identity — load it before any Paperclip call"


def _new_section() -> str:
    """The current section, sliced from the live template so this script and a
    fresh seed cannot disagree."""
    start = _AGENTS_TEMPLATE.index(_NEW_HEADING)
    nxt = _AGENTS_TEMPLATE.find("\n## ", start)
    return _AGENTS_TEMPLATE[start : len(_AGENTS_TEMPLATE) if nxt == -1 else nxt + 1]


def rewrite(content: str) -> tuple[str | None, str]:
    """Return ``(new_content, reason)``. ``new_content`` is None when nothing
    should be written — already healed, nothing to do, or not safely
    recognisable."""
    if _NEW_HEADING in content and _LEGACY_HEADING not in content:
        return None, "already healed"
    start = content.find(_LEGACY_HEADING)
    if start == -1:
        if "jq -r .token" in content or "paperclip-claimed-api-key.json" in content:
            # Mentions the credential but not under a heading we recognise.
            # Do not guess at boundaries — surface it instead.
            return None, "MANUAL: mentions the key file but no recognised section"
        return None, "nothing to do"
    nxt = content.find("\n## ", start)
    end = len(content) if nxt == -1 else nxt + 1
    return content[:start] + _new_section() + content[end:], "rewritten"


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill the Paperclip-identity section.")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    workspace = Workspace()
    rows = await container_repo.scan_all()
    logger.info("owners: %d  mode: %s", len(rows), "APPLY" if args.apply else "DRY RUN")

    counts: dict[str, int] = {}
    for row in rows:
        owner_id = row["owner_id"]
        try:
            config = json.loads(workspace.read_file(owner_id, "openclaw.json"))
        except Exception as exc:  # noqa: BLE001 — report and continue
            logger.info("%s  SKIP no openclaw.json (%s)", owner_id, exc)
            counts["no config"] = counts.get("no config", 0) + 1
            continue
        for entry in config.get("agents", {}).get("list", []) or []:
            agent_id = entry.get("id")
            if not agent_id:
                continue
            rel = f"workspace-{agent_id}/AGENTS.md"
            try:
                content = workspace.read_file(owner_id, rel)
            except Exception:  # noqa: BLE001 — an agent with no playbook is fine
                continue
            new_content, reason = rewrite(content)
            counts[reason] = counts.get(reason, 0) + 1
            if new_content is None:
                if reason.startswith("MANUAL"):
                    logger.info("%s  %s  %s", owner_id, agent_id, reason)
                continue
            logger.info("%s  %s  %s", owner_id, agent_id, reason)
            if args.apply:
                workspace.write_file(owner_id, rel, new_content)
    logger.info("summary: %s", counts)


if __name__ == "__main__":
    asyncio.run(main())
