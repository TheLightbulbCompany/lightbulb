"""Reapply the current default ``skills.external_dirs`` to existing Hermes profiles.

``hermes_profiles.build_profile_config`` writes ``skills.external_dirs`` once, at profile
creation. Changing the module constant (``hermes_profiles.SKILLS_EXTERNAL_DIRS``) only affects
FRESH profiles -- an owner already provisioned keeps whatever list their profile's ``config.yaml``
was written with. This is the re-runnable ops entry point for pushing a changed default onto
profiles that already exist, through the same ``config.patch`` path
(``hermes_profiles.patch_profile_config``) ``runtime_rpc``'s RPC uses, so ``.revision`` bumps and
the bridge re-pulls on the agent's next turn. No image roll, no container restart.

It also converges auxiliary vision (owner ruling 2026-09-28: never pin it, for any provider): a
profile whose ``auxiliary.vision`` is still explicitly configured gets it unset, and an
``openai-api`` model block gets ``supports_vision`` (the one correctness-required capability
flag -- see ``hermes_profiles.auxiliary_tasks_block``), so computer_use screenshots reach the
model as images instead of an aux model's description. A gate-routed profile gets
``auxiliary.stream_only_base_urls`` (``hermes_profiles.stream_only_base_urls``), without which
every non-streaming aux call -- vision_analyze included -- is a 400 at the gate.

Runs INSIDE the backend task (ECS exec), like ``roll_hermes_runtimes``:

    uv run python -m scripts.reapply_hermes_skills_dirs --owner-id <org> [--dry-run]
    uv run python -m scripts.reapply_hermes_skills_dirs --all [--dry-run]

Idempotent: reads each profile's stored ``skills.external_dirs`` before writing and skips it
when it already matches the current default. Prints one line per agent (``current`` /
``updated`` / ``would-update``). Sequential; continues past a failing owner/agent and exits
non-zero if any failed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from core.dynamodb import run_in_thread
from core.repositories import container_repo
from core.services.runtime_transport import runtime_arn
from core.services.hermes_profiles import (
    AUX_VISION_UNSET,
    SKILLS_EXTERNAL_DIRS,
    patch_profile_config,
    read_profile_config,
    stream_only_base_urls,
)
from core.services.messaging import load_agent_roster

logger = logging.getLogger(__name__)


async def _targets(args: argparse.Namespace) -> list[str]:
    if args.owner_id:
        return [args.owner_id]
    rows = await container_repo.scan_all(consistent=True)
    # A row without a runtime ARN has nothing to roll -- there is no other transport to skip.
    return sorted(r["owner_id"] for r in rows if runtime_arn(r) and r.get("owner_id"))


def _apply_one(owner_id: str, agent_id: str, *, dry_run: bool) -> str:
    """Sync S3 work (matches ``patch_profile_config``'s own convention) -- call via
    ``run_in_thread``, never directly from a coroutine."""
    config = read_profile_config(owner_id, agent_id)
    patch: dict = {}
    if (config.get("skills") or {}).get("external_dirs") != SKILLS_EXTERNAL_DIRS:
        patch["skills"] = {"external_dirs": list(SKILLS_EXTERNAL_DIRS)}
    vision = (config.get("auxiliary") or {}).get("vision") or {}
    # Hermes's own "explicit override" test (vision_routing._explicit_aux_vision_override) --
    # applies to every provider now, not just the vision-reading ones.
    if str(vision.get("provider") or "auto") != "auto" or vision.get("model") or vision.get("base_url"):
        patch["auxiliary"] = {"vision": dict(AUX_VISION_UNSET)}
    streamed = stream_only_base_urls((config.get("model") or {}).get("base_url") or "")
    if streamed and (config.get("auxiliary") or {}).get("stream_only_base_urls") != streamed:
        patch.setdefault("auxiliary", {})["stream_only_base_urls"] = streamed
    provider = (config.get("model") or {}).get("provider")
    if provider == "openai-api" and (config.get("model") or {}).get("supports_vision") is not True:
        patch["model"] = {"supports_vision": True}
    if not patch:
        return "current"
    if dry_run:
        return f"would-update {sorted(patch)}"
    patch_profile_config(owner_id, agent_id, patch)
    return "updated"


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--owner-id")
    group.add_argument("--all", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    failed = 0
    for owner_id in await _targets(args):
        try:
            agent_ids = [agent["id"] for agent in await load_agent_roster(owner_id)]
        except Exception as exc:  # noqa: BLE001 -- one owner's failure must not stop the fleet run
            failed += 1
            logger.error("reapply: owner=%s roster lookup FAILED: %s: %s", owner_id, type(exc).__name__, exc)
            continue
        for agent_id in agent_ids:
            try:
                result = await run_in_thread(_apply_one, owner_id, agent_id, dry_run=args.dry_run)
            except Exception as exc:  # noqa: BLE001
                failed += 1
                logger.error("reapply: owner=%s agent=%s FAILED: %s: %s", owner_id, agent_id, type(exc).__name__, exc)
                continue
            print(f"{owner_id} {agent_id} {result}")
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(main()))
