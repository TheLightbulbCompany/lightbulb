"""One-shot ops: regenerate an owner's ``openclaw.json`` on EFS from the
current config generator.

Per-user OpenClaw containers keep their PROVISION-TIME ``openclaw.json`` on
EFS. A code deploy that changes the config GENERATOR
(``core/containers/config.py``) does NOT touch an existing container's EFS
file, and scaling ``desiredCount`` 0->1 just relaunches the task reading the
stale config. This script pushes the regenerated config onto an existing
owner's container by calling the SAME write path provisioning uses
(``ecs_manager.write_user_configs``), which rewrites ``openclaw.json`` while
preserving the EFS workspace / sessions (it only writes config + device
files; it never deletes agent workspaces — see ``write_user_configs`` and
``ensure_device_identities``).

The motivating change is the model-cost remediation: the regenerated config
demotes the default agent to the cheaper standard tier (Sonnet on the
Bedrock path), pins the CEO (Friday) to the premium tier, sets
``agents.defaults.params.cacheRetention = "long"``, and routes aux
operations (compaction / memory-flush / heartbeat) to the cheap tier. All of
that comes from ``build_openclaw_config_dict`` — this script just re-renders
and re-writes; it does not duplicate any of that assembly logic.

Runtime-appended state is PRESERVED across the regen
----------------------------------------------------

``write_user_configs`` writes a FRESH ``build_openclaw_config_dict()`` over
the EFS file. Some openclaw.json subtrees are NOT produced by the generator
— they are appended at runtime AFTER provision and would be silently
clobbered by a blind overwrite. The generator emits ``channels.<provider>``
with only provider defaults (``enabled`` / ``dmPolicy``), but channel
pairing writes ``channels.<provider>.accounts.<agent_id>.allowFrom`` at
runtime (``channel_link_service.complete_link`` via ``config_patcher``),
plus a matching top-level ``bindings`` route entry. The generator emits
neither ``accounts``, ``bindings``, nor the top-level ``identity`` OpenClaw
adds at boot.

So this script reads the FULL on-disk config BEFORE the regen, runs the
full regen (which applies the new model tiers / params / aux AND resolves
the correct live ``agents.list`` / integration ``mcp.servers``), re-reads
what the regen actually landed, then DEEP-DIFFS the two: any subtree the
regen dropped (``_runtime_only_delta``) is re-added via
``config_patcher.patch_openclaw_config`` — a deep-merge over the fresh
config. The diff is general — it preserves EVERY runtime-only subtree the
generator omits, not a hardcoded allowlist, so a future runtime-only subtree
survives without a code change here. And because the delta holds only keys
the regen did NOT set, the deep-merge can never revert a freshly generated
value (the new model refs / cost blocks / aux-model slots win; only
``accounts`` / ``bindings`` / ``identity`` and friends are re-added). Net
effect: linked Slack/Discord/Telegram DMs keep routing AND the new model
config lands.

Run via::

    cd apps/backend
    uv run python -m scripts.reapply_openclaw_config \\
        --env prod --owner-id org_xxx
    uv run python -m scripts.reapply_openclaw_config \\
        --env prod --owner-id org_xxx --apply

Defaults to dry-run; pass ``--apply`` to actually call
``write_user_configs``. The dry run resolves everything (the owner's
``provider_choice``, the gateway/hook tokens off the containers row, the
target EFS path) and logs exactly what WOULD be written, but does not call
the write path.

Idempotent + safe to re-run: ``write_user_configs`` reuses the existing
gateway token, EFS access point, node/operator device identities, and agent
workspaces; it only overwrites the config + paired-device files. Re-running
just re-renders the same config from the same inputs, and the
runtime-subtree restore is itself a dedup-safe deep-merge.

Note on rollout: re-writing ``openclaw.json`` lands the new config on EFS,
but a RUNNING container only picks it up via OpenClaw's file watcher
(``CHOKIDAR_USEPOLLING``) or on its next task restart. This script does not
restart the task — an operator can force a redeploy separately
(``reprovision_for_user``) if the watcher pickup is not sufficient.

Adaptation notes
----------------

* The container scope is the ``owner_id`` (``org_*`` for orgs, ``user_*``
  for personal owners) — the same key the ``containers`` table and
  ``write_user_configs`` use. The ``user_id`` parameter name on
  ``write_user_configs`` is historical; it is treated as ``owner_id``
  internally (see its docstring).
* ``provider_choice`` (+ ``byo_provider``) is resolved exactly the way the
  background provision path does, via
  ``container_provision.resolve_provider_choice`` (reads ``billing_repo``,
  falls back to ``bedrock_claude``). We do NOT re-derive it here.
* ``gateway_token`` and ``hook_token`` are read off the existing
  ``containers`` row so the rendered config + device trust store stay
  consistent with the live task-def env. If the row is missing a
  ``gateway_token`` (legacy rows), we refuse rather than mint a new one — a
  fresh token would diverge from the token already baked into the running
  task and silently break gateway auth. Rotating tokens is the provisioning
  path's job, not this re-render script's.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger("reapply_openclaw_config")


def _runtime_only_delta(existing: dict | None, fresh: dict | None) -> dict:
    """Deep-diff ``existing`` against the freshly regenerated ``fresh`` and
    return only the subtrees the regen DROPPED — i.e. everything present in
    ``existing`` that ``fresh`` does not produce.

    This is the general form of "preserve runtime-appended config." The
    config generator (``build_openclaw_config_dict``) emits an authoritative
    config for every key it knows about, but several openclaw.json subtrees
    are written at runtime AFTER provision and the generator never emits
    them — a full overwrite drops them. Rather than hardcode an allowlist of
    those subtrees (which silently loses any new runtime-only subtree the
    moment one is added), we diff the regen output against the on-disk file
    and re-add only what the regen left out. Examples of what this preserves:

    * ``channels.<provider>.accounts`` — channel pairing state written by
      ``channel_link_service`` (allowFrom peers). The generator emits the
      provider with ``enabled``/``dmPolicy`` only, so ``accounts`` is dropped
      by an overwrite and linked DMs stop routing until re-pair.
    * top-level ``bindings`` — the route entries written alongside each
      channel account. Without them the bot misroutes inbound channel
      messages to the default agent.
    * top-level ``identity`` and any other subtree OpenClaw adds at boot.

    Because the delta contains ONLY keys the regen did not set, deep-merging
    it back over the fresh config can never revert a regenerated value
    (e.g. the new ``agents.defaults.model`` / cost blocks / aux-model slots
    win — they are present in ``fresh`` so they are excluded from the delta).

    Diff rules:

    * A key present in ``existing`` but absent in ``fresh`` is preserved
      whole (the regen does not own it).
    * When both have a key holding a dict, recurse — preserve only the nested
      keys the regen omits.
    * Any non-dict value the regen set (scalar OR list, even when its value
      changed) is owned by the fresh write and excluded. Lists are not
      descended into: the regen rebuilds e.g. ``agents.list`` /
      ``models.providers[*].models`` wholesale, so the fresh list is
      authoritative.

    Returns an empty dict when the regen is a superset of ``existing``
    (nothing was dropped); the caller skips the restore patch entirely then.
    """
    if not isinstance(existing, dict) or not isinstance(fresh, dict):
        return {}

    delta: dict = {}
    for key, existing_value in existing.items():
        if key not in fresh:
            # The regen does not emit this key at all — preserve it whole.
            delta[key] = existing_value
            continue
        fresh_value = fresh[key]
        if isinstance(existing_value, dict) and isinstance(fresh_value, dict):
            nested = _runtime_only_delta(existing_value, fresh_value)
            if nested:
                delta[key] = nested
        # else: the regen owns this key (scalar/list, or a type the regen
        # changed) — exclude it so the restore can't revert the fresh value.
    return delta


async def main(*, apply: bool, env: str, owner_id: str) -> int:
    # Imports inside main so script setup (path, env vars) is done first —
    # matches the backfill script's lazy-import convention.
    from pathlib import Path

    from core.config import settings
    from core.containers import get_ecs_manager
    from core.containers.config import read_openclaw_config_from_efs
    from core.repositories import container_repo
    from core.services.config_patcher import patch_openclaw_config
    from core.services.container_provision import resolve_provider_choice

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    container = await container_repo.get_by_owner_id(owner_id)
    if not container:
        print(f"[{env}] No container row for owner {owner_id} — nothing to reapply")
        return 1

    gateway_token = container.get("gateway_token")
    if not gateway_token:
        # Legacy rows pre-dating gateway_token storage. Minting a fresh one
        # here would diverge from the token already baked into the running
        # task-def, breaking gateway auth. Refuse — the provisioning path
        # owns token minting.
        print(
            f"[{env}] Container row for {owner_id} has no gateway_token — "
            "refusing to reapply (run a full reprovision instead)"
        )
        return 1

    hook_token = container.get("hook_token")
    owner_type = container.get("owner_type") or "personal"

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)

    config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"

    print(f"[{env}] Reapply openclaw.json for owner {owner_id}")
    print(f"  provider_choice : {provider_choice}")
    print(f"  byo_provider    : {byo_provider}")
    print(f"  owner_type      : {owner_type}")
    print(f"  target EFS path : {config_path}")
    print(
        "  new config carries: demoted default model (standard/Sonnet on "
        "bedrock), CEO premium override, cacheRetention=long, aux/cheap "
        "models for compaction+memory-flush+heartbeat (all from the updated "
        "generator build_openclaw_config_dict)"
    )

    if not apply:
        print("DRY RUN — pass --apply to write the regenerated openclaw.json")
        return 0

    try:
        # Snapshot the FULL on-disk config BEFORE the regen overwrites it.
        # The regen renders an authoritative config (correct agents.list,
        # new model tiers / params / aux models) but drops every subtree the
        # generator does not emit — channel pairing accounts/allowFrom, route
        # bindings, identity, and any other runtime-appended state.
        existing = await read_openclaw_config_from_efs(owner_id)

        # write_user_configs resolves the live Paperclip agent list +
        # connected integration MCP servers, so the fresh agents.list /
        # mcp.servers are correct — that resolution is exactly why we go
        # through the provisioning writer rather than rebuilding the dict in
        # this script.
        await get_ecs_manager().write_user_configs(
            owner_id,
            gateway_token,
            provider_choice=provider_choice,
            byo_provider=byo_provider,
            hook_token=hook_token,
            owner_type=owner_type,
        )
        print(f"  OK   wrote {config_path}")

        # Re-read what the regen actually landed, then deep-diff it against
        # the pre-regen snapshot to recover ONLY the subtrees the regen
        # dropped. Diffing (vs. a hardcoded allowlist) preserves any
        # runtime-only subtree without a code change, and — because the
        # delta holds only keys the regen did not set — re-adding it can
        # never revert a regenerated value.
        fresh = await read_openclaw_config_from_efs(owner_id)
        restore_patch = _runtime_only_delta(existing, fresh)

        if restore_patch:
            # Deep-merge the dropped runtime subtrees back over the fresh
            # config. patch_openclaw_config re-adds the matching channel
            # route bindings in the same atomic write.
            await patch_openclaw_config(owner_id, restore_patch)
            restored = ", ".join(sorted(restore_patch.keys()))
            print(f"  OK   restored runtime subtrees: {restored}")
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
        help="Environment label (informational; doesn't change DDB/EFS targeting).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the regenerated config (default: dry-run).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(apply=args.apply, env=args.env, owner_id=args.owner_id)))
