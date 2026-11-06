"""Move existing workspaces' starter agents onto the current ``STARTER_CAPABILITIES`` text.

Capabilities are written to the Paperclip agent row only at provisioning, and
``hermes_profiles.build_soul_md`` renders them into the agent's SOUL.md, so a change to a starter's
text reaches new workspaces only. This patches each existing starter (``OLD_CAPABILITIES``: the CEO,
Growth) whose capabilities are still exactly its OLD seeded text, then refreshes its SOUL
(``refresh_agent_soul``, the same step ``PATCH /teams/agents/{id}`` runs; the agent registry row
follows the Paperclip write). Text the owner edited is left alone.

Runs INSIDE the backend task (ECS exec), like ``refresh_owner_facts``:

    uv run python -m scripts.refresh_starter_ceo_capabilities --owner-id <org> [--apply]
    uv run python -m scripts.refresh_starter_ceo_capabilities --all [--apply]

Default is a dry run (reads only). Sequential; one workspace's failure never stops the run.
Exit 1 if any workspace failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys

from core.repositories import container_repo
from core.services.hermes_provisioning import refresh_agent_soul
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_provisioning import (
    STARTER_CAPABILITIES,
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.user_provision import ceo_agent_id

#: The starter CEO's capabilities as seeded before PR #4475 (origin/main 5bb96224e). Kept as a
#: literal, not imported: once #4475 lands the constant holds the NEW text, and only an exact match
#: of this one proves the owner never edited it.
OLD_CEO_CAPABILITIES = (
    "Sets direction, splits work across the team, reports back."
    "\n\n"
    "Owns the company's goals and the plan. When the owner asks for something, first restate the "
    "outcome in one line, then decide who does it: do it yourself only if it's a decision or a "
    "summary, otherwise create a task for the right specialist and tell the owner who has it. Keeps "
    "BRIEF.md and the goals current, checks on stalled tasks, and posts a short progress note when "
    "work lands. Tools when connected: Google Workspace or Microsoft 365 (calendar, mail, docs), "
    "Notion, Linear."
)
#: Growth before PR #4926 (origin/main 21aa6aee6^): it proposed "one experiment with a success number", so its
#: summaries invented targets (dev walk 15).
OLD_GROWTH_CAPABILITIES = (
    "Finds and wins customers; owns marketing, content, outreach and campaigns."
    "\n\n"
    "Owns getting attention and turning it into customers. First step: ask who the customer is and "
    "where they already are, then propose one experiment with a success number before producing "
    "anything. Writes posts, landing copy, emails and outreach lists; runs campaigns only with the "
    "owner's approval. Tools when connected: X, Instagram, Meta Ads, Apollo, Attio, Google "
    "Workspace. Hands results to Analytics to measure, site changes to Engineering."
)
#: Starter key -> the text it was seeded with before its last change. A starter not here never changed.
OLD_CAPABILITIES = {"ceo": OLD_CEO_CAPABILITIES, "growth": OLD_GROWTH_CAPABILITIES}


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    return sorted({r["owner_id"] for r in rows if r.get("owner_id")})


async def _refresh_one(provisioning, cookie: str, owner_id: str, apply: bool) -> str:
    row = await provisioning._repo.resolve_owner_row(owner_id)
    # Same guard as arm_nightly_ceo: only a committed company that belongs to this workspace.
    if row is None or row.status != "active" or row.org_id != owner_id:
        return "skipped (no active company)"
    results = [await _refresh_starter(provisioning, cookie, owner_id, row, key, apply) for key in OLD_CAPABILITIES]
    return "; ".join(results)


async def _refresh_starter(provisioning, cookie: str, owner_id: str, row, key: str, apply: bool) -> str:
    old, new = OLD_CAPABILITIES[key], STARTER_CAPABILITIES[key]
    if new == old:
        return f"skipped (STARTER_CAPABILITIES[{key!r}] is still the old text)"
    agent_id = ceo_agent_id(row) if key == "ceo" else (row.starter_agent_ids or {}).get(key)
    if not agent_id:
        return f"skipped (no starter {key})"
    admin = provisioning._admin
    agent = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
    current = (agent or {}).get("capabilities")
    if current == new:
        if not apply:
            return f"skipped (already current) {key}={agent_id}"
        # Repairs a SOUL whose refresh failed after an earlier run's patch landed.
        await refresh_agent_soul(owner_id, agent_id)
        return f"skipped (already current, SOUL refreshed) {key}={agent_id}"
    if current != old:
        return f"skipped (capabilities edited) {key}={agent_id}"
    if not apply:
        return f"would update {key}={agent_id}"
    await admin.update_agent(session_cookie=cookie, agent_id=agent_id, capabilities=new)
    await refresh_agent_soul(owner_id, agent_id)
    return f"updated {key}={agent_id}"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true", help="patch (default: dry run)")
    args = parser.parse_args(argv)

    counts: dict[str, int] = {}
    provisioning = await build_paperclip_provisioning()
    try:
        cookie = await get_admin_session_cookie(provisioning._admin._http)
        for owner_id in await _targets(args):
            try:
                result = await _refresh_one(provisioning, cookie, owner_id, args.apply)
            except Exception as exc:  # noqa: BLE001 -- one workspace's failure must not stop the run
                result = f"failed ({type(exc).__name__}: {exc})"
            print(f"{owner_id}: {result}")
            for part in result.split("; "):  # one per starter: "updated growth=…", "skipped (…) ceo=…"
                key = re.sub(r" \w+=\S+$", "", part.split(" (")[0])
                counts[key] = counts.get(key, 0) + 1
    finally:
        await close_paperclip_provisioning(provisioning)
    print("total: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
