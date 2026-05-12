"""EFS config patcher — file-locked, atomic, deep-merge patching of openclaw.json."""

import asyncio
import copy
import fcntl
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Callable

from core.config import settings

logger = logging.getLogger(__name__)

_efs_mount_path = settings.EFS_MOUNT_PATH


class ConfigPatchError(Exception):
    pass


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge patch into base. Dicts are merged recursively. Non-dict values are replaced."""
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


async def _locked_rmw(
    owner_id: str,
    mutate_fn: Callable[[dict], bool],
    log_context: str,
) -> None:
    """Locked read-modify-write skeleton for openclaw.json on EFS.

    Resolves the owner's config path, raises ConfigPatchError if missing,
    acquires an exclusive fcntl.lockf on openclaw.json, reads + parses JSON,
    invokes ``mutate_fn(current)`` to apply per-call mutation logic in place.

    ``mutate_fn`` returns True if the config was modified and a write is
    needed, or False to signal a no-op (skip backup + write entirely).

    On a write, takes a .bak backup, validates serializability, atomically
    renames a tempfile into place, and chowns to uid/gid 1000 if running
    as root. Releases the lock in a finally block. Logs success with
    ``log_context``.
    """
    config_dir = os.path.join(_efs_mount_path, owner_id)
    config_path = os.path.join(config_dir, "openclaw.json")
    backup_path = os.path.join(config_dir, "openclaw.json.bak")

    def _do_rmw():
        lock_fd = None
        try:
            try:
                lock_fd = open(config_path, "r+")
            except FileNotFoundError:
                raise ConfigPatchError(f"Config not found for owner {owner_id}")
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)

            # Read from the same fd we hold the lock on (avoid TOCTOU + double-open).
            lock_fd.seek(0)
            current = json.load(lock_fd)

            changed = mutate_fn(current)
            if not changed:
                return  # no-op: skip backup + write

            shutil.copy2(config_path, backup_path)
            json.dumps(current)  # validate serializable

            fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(current, f, indent=2)
                # Match OpenClaw container ownership (node uid=1000 gid=1000).
                # Only chown if running as root (backend container runs as root,
                # but tests run as the local user).
                if os.getuid() == 0:
                    os.chown(tmp_path, 1000, 1000)
                os.rename(tmp_path, config_path)
            except Exception:
                os.unlink(tmp_path)
                raise

            logger.info("openclaw.json for owner %s: %s", owner_id, log_context)
        finally:
            if lock_fd:
                fcntl.lockf(lock_fd, fcntl.LOCK_UN)
                lock_fd.close()

    await asyncio.to_thread(_do_rmw)


def _ensure_channel_bindings_for_patch(cfg: dict, patch: dict) -> None:
    """Mutate ``cfg`` in place to add a route binding for every
    ``channels.<provider>.accounts.<agent_id>`` introduced by ``patch``.

    OpenClaw's dispatcher (desktop/openclaw src/routing/bindings.ts) routes
    ``(channel, accountId)`` via the top-level ``bindings`` array; without a
    matching entry, messages fall through to the default agent. Every
    channel account needs a sibling binding or the bot misroutes to ``main``.

    Dedupe-safe: binding equality is structural (Python ``dict ==``), so
    repeated patches are no-ops. Shape matches the zod schema at
    ``desktop/openclaw src/config/zod-schema.agents.ts:37-44``.
    """
    channels = patch.get("channels")
    if not isinstance(channels, dict):
        return
    bindings = cfg.get("bindings")
    if not isinstance(bindings, list):
        bindings = []
        cfg["bindings"] = bindings
    for provider, provider_cfg in channels.items():
        if not isinstance(provider_cfg, dict):
            continue
        accounts = provider_cfg.get("accounts")
        if not isinstance(accounts, dict):
            continue
        for agent_id, account_cfg in accounts.items():
            if not isinstance(account_cfg, dict):
                continue
            new_binding = {
                "type": "route",
                "agentId": agent_id,
                "match": {"channel": provider, "accountId": agent_id},
            }
            if new_binding not in bindings:
                bindings.append(new_binding)


async def replace_provider_block(
    owner_id: str,
    *,
    providers: dict,
    default_model: dict,
    plugin_entries: dict,
) -> None:
    """Atomically swap the three openclaw.json subtrees that define the LLM
    provider: ``models.providers``, ``agents.defaults.model``, and
    ``plugins.entries``.

    Used by the post-onboarding change-provider hot path
    (``billing_service.change_provider`` for Bedrock ↔ ChatGPT switches).
    A deep-merge can't express "replace this subtree" — merging an empty
    ``providers`` dict over an existing ``{"amazon-bedrock": ...}`` would
    leave the old entry in place. So we do leaf-level *replacement* on
    each of the three keys.

    All three replacements happen inside a single ``_locked_rmw`` write
    so chokidar (running with ``CHOKIDAR_USEPOLLING=true`` on EFS) fires
    exactly one reload event. OpenClaw's reload-plan classifies all
    three keys as ``kind: "hot"`` with actions ``restart-heartbeat`` +
    ``reload-plugins`` (see openclaw/src/gateway/config-reload-plan.ts),
    so the gateway picks up the new provider without a process restart.

    Intermediate dicts (``models``, ``agents``, ``agents.defaults``,
    ``plugins``) are created or coerced as needed — a partial existing
    structure from a legacy config doesn't block the swap.

    Raises ``ConfigPatchError`` (via ``_locked_rmw``) if the owner has
    no ``openclaw.json`` on EFS yet. Callers should fall back to a full
    re-provision in that case.
    """

    def _mutate(current: dict) -> bool:
        models = current.get("models")
        if not isinstance(models, dict):
            models = {}
            current["models"] = models
        models["providers"] = copy.deepcopy(providers)

        agents = current.get("agents")
        if not isinstance(agents, dict):
            agents = {}
            current["agents"] = agents
        defaults = agents.get("defaults")
        if not isinstance(defaults, dict):
            defaults = {}
            agents["defaults"] = defaults
        defaults["model"] = copy.deepcopy(default_model)

        plugins = current.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            current["plugins"] = plugins
        plugins["entries"] = copy.deepcopy(plugin_entries)

        return True

    await _locked_rmw(owner_id, _mutate, "replace provider block")


async def patch_openclaw_config(owner_id: str, patch: dict) -> None:
    """Patch openclaw.json on EFS with file locking and atomic write.

    Deep-merges the patch into the existing config and writes the result
    atomically. Raises ConfigPatchError if the config file doesn't exist.
    Always writes (even for empty patches) to preserve historical behavior.

    Channel invariant: every ``channels.<provider>.accounts.<agent_id>``
    introduced by the patch also gets a matching top-level ``bindings``
    entry in the same atomic write. Required by OpenClaw's inbound-message
    dispatcher (desktop/openclaw src/routing/bindings.ts) or the bot
    misroutes to the default agent (``main``).
    """

    def _mutate(current: dict) -> bool:
        merged = _deep_merge(current, patch)
        current.clear()
        current.update(merged)
        # Apply within the same locked RMW so account block + route binding
        # land atomically — no window where one exists without the other.
        _ensure_channel_bindings_for_patch(current, patch)
        return True  # always write, even for empty patches

    await _locked_rmw(owner_id, _mutate, f"patch keys={list(patch.keys())}")


async def append_to_openclaw_config_list(
    owner_id: str,
    path: list[str],
    value,
) -> None:
    """Append `value` to the list at `path` in the owner's openclaw.json.

    Semantics:
    - If `path` doesn't exist, create nested dicts as needed and initialize the
      list with `[value]`.
    - If the list already contains `value`, this is a no-op (dedup).
    - Acquires the same fcntl.lockf exclusive lock on openclaw.json as
      `patch_openclaw_config` to serialize concurrent writes.
    """
    if not path:
        raise ConfigPatchError("path must not be empty")

    def _mutate(current: dict) -> bool:
        cursor = current
        for segment in path[:-1]:
            if segment not in cursor or not isinstance(cursor[segment], dict):
                cursor[segment] = {}
            cursor = cursor[segment]

        leaf_key = path[-1]
        existing = cursor.get(leaf_key)
        if not isinstance(existing, list):
            cursor[leaf_key] = [value]
            return True
        if value in existing:
            return False  # dedup no-op
        existing.append(value)
        return True

    await _locked_rmw(owner_id, _mutate, f"append path={path} value={value!r}")


async def remove_from_openclaw_config_list(
    owner_id: str,
    path: list[str],
    predicate: Callable[[Any], bool],
) -> None:
    """Remove entries from the list at `path` where `predicate(entry)` returns True.

    Semantics:
    - If `path` doesn't exist or isn't a list, no-op (no error).
    - Predicate-match approach supports both string matching (e.g. allowFrom)
      and structural matching (e.g. bindings dict entries).
    - Acquires the same fcntl.lockf exclusive lock as append_to_openclaw_config_list.
    """
    if not path:
        raise ConfigPatchError("path must not be empty")

    def _mutate(current: dict) -> bool:
        cursor = current
        for segment in path[:-1]:
            if segment not in cursor or not isinstance(cursor[segment], dict):
                return False  # missing path, no-op
            cursor = cursor[segment]

        leaf_key = path[-1]
        existing = cursor.get(leaf_key)
        if not isinstance(existing, list):
            return False  # not a list or missing, no-op

        try:
            filtered = [item for item in existing if not predicate(item)]
        except Exception as exc:
            raise ConfigPatchError(f"predicate raised while filtering path={path}: {exc}") from exc
        if len(filtered) == len(existing):
            return False  # nothing removed, skip the write

        cursor[leaf_key] = filtered
        return True

    await _locked_rmw(owner_id, _mutate, f"remove path={path}")


async def apply_deploy_mutation(
    owner_id: str,
    agent_entry: dict,
    plugins_patch: dict,
    *,
    mcp_patch: dict | None = None,
    skills_patch: dict | None = None,
) -> None:
    """Atomically apply a catalog-deploy mutation to openclaw.json.

    Performed inside a single exclusive lock so concurrent deploys cannot
    race on the read-modify-write of the agents list:
      1. Append `agent_entry` to `config["agents"]["list"]`.
      2. Deep-merge `plugins_patch` into `config["plugins"]`.
      3. (Optional) Deep-merge ``mcp_patch`` (e.g. ``{"servers": {…}}``)
         into ``config["mcp"]``. Used by marketplace deploys to register
         the selected MCP servers from the publisher's slice.
      4. (Optional) Deep-merge ``skills_patch.entries`` and union
         ``skills_patch.allowBundled`` into ``config["skills"]``. Used by
         marketplace deploys to register config-keyed skills + image-baked
         bundled skills the publisher selected.

    NOTE: top-level ``tools`` is intentionally left alone. The catalog code
    used to write ``tools.allowed = []``, which is not a valid key in
    OpenClaw's ``ToolsSchema`` (``.strict()`` at
    openclaw/src/config/zod-schema.agent-runtime.ts:911) — that wrote a junk
    field that caused chokidar's hot-reload to reject the entire config with
    ``Unrecognized key: "allowed"``, leaving every deployed agent invisible
    in the running container. Per-agent tool policy lives on the agent entry
    itself (``AgentToolsSchema``), which is already carried through the
    slice. Top-level tools are global to the container and shouldn't be
    silently mutated by a deploy.

    Self-heals: if a stale ``tools.allowed`` is present from before this fix,
    drop it on the next mutation so the file becomes valid again.
    """

    def _mutate(current: dict) -> bool:
        # 1. Append agent entry to agents.list (always mutates — each deploy
        # gets a fresh id). OpenClaw schema: agents = {defaults, list}. See
        # openclaw/src/config/zod-schema.agents.ts.
        agents_obj = current.get("agents")
        if isinstance(agents_obj, list):
            # Legacy flat-list shape from pre-schema-fix deploys: migrate by
            # promoting the list under `agents.list` so existing entries
            # survive rather than getting silently clobbered.
            agents_obj = {"list": list(agents_obj)}
        elif not isinstance(agents_obj, dict):
            agents_obj = {}
        agents_list = agents_obj.get("list")
        if not isinstance(agents_list, list):
            agents_list = []
        agents_list.append(copy.deepcopy(agent_entry))
        agents_obj["list"] = agents_list
        current["agents"] = agents_obj

        # 2. Deep-merge plugins.
        if plugins_patch:
            existing_plugins = current.get("plugins")
            if not isinstance(existing_plugins, dict):
                existing_plugins = {}
            current["plugins"] = _deep_merge(existing_plugins, plugins_patch)

        # 3. Deep-merge mcp servers (marketplace bundle of selected MCPs).
        if mcp_patch:
            existing_mcp = current.get("mcp")
            if not isinstance(existing_mcp, dict):
                existing_mcp = {}
            current["mcp"] = _deep_merge(existing_mcp, mcp_patch)

        # 4. Merge skills.entries (deep) + skills.allowBundled (union).
        if skills_patch:
            existing_skills = current.get("skills")
            if not isinstance(existing_skills, dict):
                existing_skills = {}

            # Pull allowBundled out and union manually so we don't get
            # duplicate entries from _deep_merge's list-replace semantics.
            patch_bundled = skills_patch.get("allowBundled")
            patch_minus_bundled = {k: v for k, v in skills_patch.items() if k != "allowBundled"}
            if patch_minus_bundled:
                existing_skills = _deep_merge(existing_skills, patch_minus_bundled)
            if isinstance(patch_bundled, list):
                existing_bundled = existing_skills.get("allowBundled")
                if not isinstance(existing_bundled, list):
                    existing_bundled = []
                # Union, preserving caller order for new additions.
                merged_bundled = list(existing_bundled)
                for name in patch_bundled:
                    if name not in merged_bundled:
                        merged_bundled.append(name)
                existing_skills["allowBundled"] = merged_bundled
            current["skills"] = existing_skills

        # 5. Self-heal: strip the invalid ``tools.allowed`` key if present
        # (left over from pre-fix deploys). Cleans up the bad on-disk state
        # so OpenClaw's strict-schema validator accepts the file again.
        tools = current.get("tools")
        if isinstance(tools, dict) and "allowed" in tools:
            del tools["allowed"]

        return True

    await _locked_rmw(
        owner_id,
        _mutate,
        f"deploy agent_id={agent_entry.get('id')!r}",
    )


async def delete_openclaw_config_path(
    owner_id: str,
    path: list[str],
) -> None:
    """Remove the key at `path` from the owner's openclaw.json entirely.

    Semantics:
    - If any intermediate path segment is missing, no-op (no error).
    - If the leaf key doesn't exist, no-op.
    - If the parent dict is empty after removal, it is left as `{}` rather
      than being pruned recursively. OpenClaw treats empty dicts the same
      as missing keys for `channels.*.accounts`.
    - Acquires the same fcntl.lockf exclusive lock as the other helpers.
    """
    if not path:
        raise ConfigPatchError("path must not be empty")

    def _mutate(current: dict) -> bool:
        cursor = current
        for segment in path[:-1]:
            if segment not in cursor or not isinstance(cursor[segment], dict):
                return False  # missing intermediate, no-op
            cursor = cursor[segment]

        leaf_key = path[-1]
        if leaf_key not in cursor:
            return False  # already absent, no-op

        del cursor[leaf_key]
        return True

    await _locked_rmw(owner_id, _mutate, f"delete path={path}")


async def set_openclaw_config_path(
    owner_id: str,
    path: list[str],
    value: Any,
) -> None:
    """Replace the leaf at `path` with `value` in the owner's openclaw.json.

    Semantics:
    - Creates missing intermediate dicts.
    - If an intermediate segment exists but is not a dict, it is replaced
      with a dict (consistent with treating the path as authoritative).
    - No-op if the leaf already equals `value` (skips write + chokidar reload).
    - Same fcntl.lockf exclusive lock as the other helpers.

    Used for "replace this entire subtree" semantics that deep-merge cannot
    express (e.g. PUT /integrations/mcp/servers wipes any server not in the
    new dict).
    """
    if not path:
        raise ConfigPatchError("path must not be empty")

    def _mutate(current: dict) -> bool:
        cursor = current
        for segment in path[:-1]:
            existing = cursor.get(segment)
            if not isinstance(existing, dict):
                cursor[segment] = {}
            cursor = cursor[segment]

        leaf_key = path[-1]
        if cursor.get(leaf_key) == value:
            return False  # already the desired value, skip the write
        cursor[leaf_key] = copy.deepcopy(value)
        return True

    await _locked_rmw(owner_id, _mutate, f"set path={path}")


async def append_cron_jobs(owner_id: str, new_jobs: list[dict]) -> None:
    """Atomically append cron jobs to ``{owner_id}/cron/jobs.json``.

    Lives in a separate file from openclaw.json (see ``config.cron.store``
    in OpenClaw's schema, default ``~/.openclaw/cron/jobs.json``), so it
    needs its own ``fcntl.lockf`` rather than reusing the openclaw.json
    lock. Same atomic-write pattern as ``_locked_rmw``: open the data
    file, take an exclusive lock, read + modify, tempfile-rename, chown.

    No-op if ``new_jobs`` is empty.

    Concurrency caveat: this matches the existing ``_locked_rmw`` pattern
    used for ``openclaw.json``, which has two known races (also untreated
    upstream) — the lock is held on the data file's inode, but the
    atomic ``os.rename`` swaps the inode out, and ``fcntl.lockf`` is
    process-wide on Linux/macOS so threads in the same process don't
    contend. In practice both races have a narrow window and the failure
    mode is "one concurrent writer's changes lost; user retries", which
    is the same posture we accept for openclaw.json today. A future PR
    should fix both with a shared lock-file helper rather than
    half-fixing only this path. Tracked separately.
    """
    if not new_jobs:
        return

    cron_dir = os.path.join(_efs_mount_path, owner_id, "cron")
    jobs_path = os.path.join(cron_dir, "jobs.json")

    def _do():
        os.makedirs(cron_dir, exist_ok=True)
        if os.getuid() == 0:
            try:
                os.chown(cron_dir, 1000, 1000)
            except OSError:
                pass
        if not os.path.exists(jobs_path):
            with open(jobs_path, "w") as f:
                json.dump({"version": 1, "jobs": []}, f)
            if os.getuid() == 0:
                try:
                    os.chown(jobs_path, 1000, 1000)
                except OSError:
                    pass

        lock_fd = open(jobs_path, "r+")
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            lock_fd.seek(0)
            try:
                data = json.load(lock_fd)
            except json.JSONDecodeError:
                data = {"version": 1, "jobs": []}
            if not isinstance(data, dict):
                data = {"version": 1, "jobs": []}
            jobs = data.get("jobs")
            if not isinstance(jobs, list):
                jobs = []
            jobs.extend(copy.deepcopy(j) for j in new_jobs)
            data["jobs"] = jobs
            data.setdefault("version", 1)

            json.dumps(data)  # validate serializability before writing

            fd, tmp_path = tempfile.mkstemp(dir=cron_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                if os.getuid() == 0:
                    os.chown(tmp_path, 1000, 1000)
                os.rename(tmp_path, jobs_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            logger.info(
                "cron/jobs.json for owner %s: appended %d job(s)",
                owner_id,
                len(new_jobs),
            )
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    await asyncio.to_thread(_do)
