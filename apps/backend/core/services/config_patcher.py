"""EFS config patcher — file-locked, atomic, deep-merge patching of openclaw.json."""

import asyncio
import copy
import fcntl
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from typing import Any, Callable

from core.config import settings
from core.containers.config import memory_plugin_entries, workflow_plugin_entries
from core.services.agent_roster import read_agent_roster, write_agent_roster
from core.services.config_schema import ConfigSchemaError, gate_config
from core.services.efs_file_lock import (
    chown_uid_1000_best_effort,
    get_file_lock,
    open_lock_fd,
    openclaw_config_sidecar_lock,
)

logger = logging.getLogger(__name__)

_efs_mount_path = settings.EFS_MOUNT_PATH


class ConfigPatchError(Exception):
    pass


# Object keys that would let an untrusted patch pollute the JS prototype chain
# when OpenClaw (TypeScript) loads + re-merges openclaw.json on the container
# side. Inert in Python's dict merge here, but never legitimate as a config key,
# so reject them at the write boundary. The pen test stored ``__proto__`` (plus
# SSTI / command-injection probe keys) via the fleet-config write — this is the
# defense-in-depth for that, independent of the now-admin-only authz fix, and it
# also guards the marketplace slice deploy (apply_deploy_aux_mutations merges
# untrusted seller plugins/mcp through this same helper).
_UNSAFE_CONFIG_KEYS = frozenset({"__proto__", "constructor", "prototype", "__reduce__", "__reduce_ex__", "__class__"})
_MAX_CONFIG_KEY_LEN = 128


def _assert_safe_patch_keys(patch: dict) -> None:
    """Recursively reject prototype-pollution, control-char, and absurdly long
    keys ANYWHERE in an incoming patch — including subtrees that the merge will
    INSERT wholesale (a new key, or a value replacing a non-dict). The merge
    recursion alone only descends when both sides are existing dicts, so without
    this walk a patch like ``{"plugins":{"entries":{"x":{"__proto__":{}}}}}``
    against a config lacking ``plugins.entries`` would copy the unsafe key in
    unchecked. Deliberately NOT a key allowlist: legit configs carry
    caller-chosen keys (MCP server names, model ids), so we only reject what is
    never a valid config key.
    """
    for key, value in patch.items():
        if not isinstance(key, str):
            raise ConfigPatchError(f"config key must be a string, got {type(key).__name__}")
        if key in _UNSAFE_CONFIG_KEYS:
            raise ConfigPatchError(f"refusing unsafe config key: {key!r}")
        if len(key) > _MAX_CONFIG_KEY_LEN:
            raise ConfigPatchError("config key exceeds maximum length")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
            raise ConfigPatchError(f"config key contains control characters: {key!r}")
        _assert_safe_patch_values(value)


def _assert_safe_patch_values(value: Any) -> None:
    """Walk nested dicts/lists so keys inside inserted/replaced subtrees (and
    dicts carried inside list entries, e.g. plugin/agent lists) are validated."""
    if isinstance(value, dict):
        _assert_safe_patch_keys(value)
    elif isinstance(value, list):
        for item in value:
            _assert_safe_patch_values(item)


def _deep_merge(base: dict, patch: dict) -> dict:
    """Deep-merge patch into base. Dicts are merged recursively; non-dict values
    replace. The ENTIRE patch (including inserted subtrees) is key-validated up
    front, so the merge recursion below can stay focused on merging."""
    _assert_safe_patch_keys(patch)
    return _merge(base, patch)


def _merge(base: dict, patch: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


async def _locked_rmw(
    owner_id: str,
    mutate_fn: Callable[[dict], bool],
    log_context: str,
    *,
    enforce_schema: bool = False,
) -> None:
    """Locked read-modify-write skeleton for openclaw.json on EFS.

    Resolves the owner's config path, raises ConfigPatchError if missing,
    acquires the guarded rollout-aware sidecar, reads + parses JSON, and
    invokes ``mutate_fn(current)`` to apply per-call mutation logic in place.

    ``mutate_fn`` returns True if the config was modified and a write is
    needed, or False to signal a no-op (skip backup + write entirely).

    On a write, takes a .bak backup, validates serializability AND the merged
    document against the vendored OpenClaw schema, atomically renames a
    tempfile into place, and chowns to uid/gid 1000 if running as root.
    Releases the lock in a finally block. Logs success with ``log_context``.

    This is the ONLY chokepoint every config writer passes through, which is why
    the schema gate lives here rather than in each caller (issue #941 — a
    schema-invalid config persists silently and crash-loops the container at its
    next restart). ``enforce_schema=True`` rejects a violating write outright;
    otherwise the gate follows ``settings.OPENCLAW_CONFIG_SCHEMA_MODE``, which
    defaults to audit. Callers merging UNTRUSTED input must pass
    ``enforce_schema=True`` — see ``apply_deploy_aux_mutations``. A rejection
    surfaces as ``ConfigPatchError`` and happens BEFORE the .bak + rename, so
    the on-disk config is untouched.

    Locking uses the shared two-layer discipline (see ``efs_file_lock``): a
    per-owner in-process ``threading.Lock`` serializes threads in this worker,
    then ``openclaw_config_sidecar_lock`` preserves the legacy fcntl protocol
    until boot recovery publishes the transition marker, after which it uses
    pinned OpenClaw's ephemeral ``O_EXCL`` protocol. The sidecar is separate
    from openclaw.json because the atomic ``os.rename`` swaps the data inode.
    """
    config_dir = os.path.join(_efs_mount_path, owner_id)
    config_path = os.path.join(config_dir, "openclaw.json")
    backup_path = os.path.join(config_dir, "openclaw.json.bak")
    lock_path = os.path.join(config_dir, "openclaw.json.lock")

    def _do_rmw():
        if not os.path.exists(config_path):
            raise ConfigPatchError(f"Config not found for owner {owner_id}")
        with openclaw_config_sidecar_lock(config_dir, lock_path):
            with open(config_path, "r") as f:
                current = json.load(f)

            changed = mutate_fn(current)
            if not changed:
                return  # no-op: skip backup + write

            # Gate BEFORE the backup + rename so a rejected write leaves both
            # openclaw.json and openclaw.json.bak exactly as they were.
            try:
                gate_config(current, owner_id=owner_id, context=log_context, enforce=enforce_schema)
            except ConfigSchemaError as exc:
                raise ConfigPatchError(str(exc)) from exc

            shutil.copy2(config_path, backup_path)
            json.dumps(current)  # validate serializable

            fd, tmp_path = tempfile.mkstemp(dir=config_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(current, f, indent=2)
                chown_uid_1000_best_effort(tmp_path)
                os.rename(tmp_path, config_path)
            except Exception:
                os.unlink(tmp_path)
                raise
            logger.info("openclaw.json for owner %s: %s", owner_id, log_context)

    def _do_rmw_locked():
        with get_file_lock(lock_path):
            _do_rmw()

    await asyncio.to_thread(_do_rmw_locked)


def _ensure_channel_bindings_for_patch(cfg: dict, patch: dict, agent_for_account: dict[str, str] | None = None) -> None:
    """Mutate ``cfg`` in place to add a route binding for every
    ``channels.<provider>.accounts.<account_id>`` introduced by ``patch``.

    OpenClaw's dispatcher (desktop/openclaw src/routing/bindings.ts) routes
    ``(channel, accountId)`` via the top-level ``bindings`` array; without a
    matching entry, messages fall through to the default agent. Every
    channel account needs a sibling binding or the bot misroutes to ``main``.

    ``agent_for_account`` maps an account id to the agent that should answer
    it (the platform Slack app keys accounts by team id and routes them to an
    agent); absent → legacy ``agentId == accountId``.

    Dedupe-safe: binding equality is structural (Python ``dict ==``), so
    repeated patches are no-ops. Shape matches the zod schema at
    ``desktop/openclaw src/config/zod-schema.agents.ts``.
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
        for account_id, account_cfg in accounts.items():
            if not isinstance(account_cfg, dict):
                continue
            mapped_agent_id = (agent_for_account or {}).get(account_id)
            if mapped_agent_id is not None:
                # Rebinding this account to a different agent must not leave
                # the OLD account-wide binding behind, or the dispatcher can
                # match either one. Only the exact account-wide shape (no
                # peer, no teamId) is removed — the legacy path below never
                # takes this branch.
                bindings[:] = [
                    b
                    for b in bindings
                    if not (isinstance(b, dict) and b.get("match") == {"channel": provider, "accountId": account_id})
                ]
                agent_id = mapped_agent_id
            else:
                agent_id = account_id
            new_binding = {
                "type": "route",
                "agentId": agent_id,
                "match": {"channel": provider, "accountId": account_id},
            }
            if new_binding not in bindings:
                bindings.append(new_binding)


async def replace_provider_block(
    owner_id: str,
    *,
    providers: dict,
    default_model: dict,
    plugin_entries: dict,
    ceo_model: str | None,
    cheap_model: str | None = None,
    heartbeat_model: str | None = None,
    utility_model: str | None = None,
    subagent_model: str | None = None,
    llm_task_model: str | None = None,
    web_search_provider: str | None = None,
    tool_search_mode: str | None = None,
    acp_block: dict | None = None,
    acpx_entry: dict | None = None,
    model_policy: dict | None = None,
) -> None:
    """Atomically swap the three openclaw.json subtrees that define the LLM
    provider: ``models.providers``, ``agents.defaults.model``, and
    ``plugins.entries`` — and re-pin the CEO agent's per-agent ``model`` plus
    the aux-model overrides under ``agents.defaults``.

    ``acp_block`` / ``acpx_entry`` keep the coding-harness config in sync
    with the new provider: the harness set is provider-derived (bedrock →
    claude, chatgpt_oauth → opencode), so a swap must replace the root
    ``acp`` block (``allowedAgents``/``defaultAgent``) and re-seed the acpx
    plugin entry — which the wholesale ``plugins.entries`` rebuild below
    would otherwise DROP, leaving the container with the old provider's ACP
    policy and no acpx runtime at all until a cold reprovision. ``None``
    leaves the root ``acp`` block untouched (and adds no acpx entry) — live
    callers always pass both.

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

    ``plugins.entries`` is rebuilt as ``workflow_plugin_entries()`` and
    ``memory_plugin_entries()`` merged with the provider entries, so the
    provider-independent bundled extensions (lobster / llm-task and
    memory-core's dreaming) persist across provider switches even though
    the old provider's entry is dropped.

    ``ceo_model`` keeps the CEO (Friday) agent's per-agent ``model`` override
    consistent with the new provider. The CEO is the ``agents.list`` entry with
    ``default: true``. When ``ceo_model`` is set, the override is (re)written to
    that ref; when it is ``None`` (a flat-rate provider with no premium tier,
    e.g. chatgpt_oauth), any existing override is *deleted* so the CEO inherits
    the new ``agents.defaults.model`` — critical for the Bedrock → ChatGPT
    switch, where leaving a stale ``amazon-bedrock/…`` ref would point Friday at
    a provider no longer in ``models.providers``. No-op when ``agents.list`` is
    absent/empty or has no default entry.

    ``cheap_model`` does the same for the compaction aux-model overrides
    provisioning writes under ``agents.defaults`` — ``compaction.model`` and
    ``compaction.memoryFlush.model``. When ``cheap_model`` is set, each slot's
    ``model`` is (re)written to it; when it is ``None`` (the Codex path, whose
    native compaction rejects an OpenClaw-side summarizer override) the
    ``model`` key is *deleted* from each slot so the aux op inherits the new
    default — same stale-ref hazard as the CEO override. Only the ``model``
    key is touched; sibling keys (``compaction.mode``, ``memoryFlush.enabled``)
    are preserved, and a slot's intermediate dict is created on demand when
    setting but left absent when clearing.

    ``heartbeat_model`` governs ``heartbeat.model`` SEPARATELY from
    ``cheap_model``: the heartbeat is an ordinary agent turn the Codex OAuth
    token CAN serve on the mini tier (unlike compaction), so on the Bedrock →
    ChatGPT swap it re-pins to ``openai/gpt-5.4-mini`` while the compaction
    slots clear. Folding it into ``cheap_model`` (the old behavior) silently
    dropped the heartbeat's cheap-tier routing on that swap. Sibling keys
    (``every``, ``isolatedSession``, ``lightContext``) are preserved.

    ``utility_model`` and ``subagent_model`` keep ``agents.defaults
    .utilityModel`` and ``agents.defaults.subagents.model`` in sync with the
    new provider — both are provider-specific refs provisioning writes (see
    ``build_openclaw_config_dict``), so a swap must re-pin or delete them like
    the CEO override. ``subagents`` sibling keys (``allowAgents``,
    ``runTimeoutSeconds``) are preserved.

    ``llm_task_model`` is forwarded to ``workflow_plugin_entries`` so the
    rebuilt ``plugins.entries['llm-task']`` carries the new provider's
    cheap-tier config (or drops back to enable-only when ``None``).

    ``web_search_provider`` keeps ``tools.web.search.provider`` in sync:
    provisioning pins ``parallel-free`` on every provider path, so live
    callers always pass it. ``None`` deletes the pin — kept as the generic
    clear, but note that an owner left with no pin has NO web search at all
    (the embedded runtime supplies none, and firecrawl_search needs an API
    key we do not set). ``tools.web.fetch`` is never touched.

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
        # modelPolicy.allow is a RESTRICTIVE allowlist, so it must track the
        # provider exactly: set it on switch TO a discoverable provider, and
        # DELETE it on switch away -- a stale ["openai/*"] after a ChatGPT ->
        # Bedrock switch rejects every amazon-bedrock model at selection time
        # and the configured default throws. See config.model_policy_block.
        if model_policy is not None:
            defaults["modelPolicy"] = copy.deepcopy(model_policy)
        else:
            defaults.pop("modelPolicy", None)

        # Keep the compaction aux-model overrides in sync with the new
        # provider — same hazard as the CEO override below, just for the
        # cheap-tier aux slots. When cheap_model is set, pin each slot's
        # `model`; when None (the Codex path — its native compaction rejects
        # an OpenClaw-side summarizer override), drop any stale override so
        # the aux op inherits the new default. Only the `model` key is touched
        # so sibling keys (compaction.mode, memoryFlush.enabled) survive. Slot
        # dicts are created on demand when setting but left absent when
        # clearing (no empty stubs).
        if cheap_model is not None:
            compaction = defaults.get("compaction")
            if not isinstance(compaction, dict):
                compaction = {}
                defaults["compaction"] = compaction
            compaction["model"] = cheap_model
            memory_flush = compaction.get("memoryFlush")
            if not isinstance(memory_flush, dict):
                memory_flush = {}
                compaction["memoryFlush"] = memory_flush
            memory_flush["model"] = cheap_model
        else:
            compaction = defaults.get("compaction")
            if isinstance(compaction, dict):
                compaction.pop("model", None)
                memory_flush = compaction.get("memoryFlush")
                if isinstance(memory_flush, dict):
                    memory_flush.pop("model", None)

        # heartbeat.model rides its OWN parameter, not cheap_model: the
        # heartbeat is an ordinary turn the Codex OAuth token can serve on
        # the mini tier, so a Bedrock -> ChatGPT swap re-pins it while the
        # compaction slots clear. Siblings (every / isolatedSession /
        # lightContext) are provider-independent and survive untouched.
        if heartbeat_model is not None:
            heartbeat = defaults.get("heartbeat")
            if not isinstance(heartbeat, dict):
                heartbeat = {}
                defaults["heartbeat"] = heartbeat
            heartbeat["model"] = heartbeat_model
        else:
            heartbeat = defaults.get("heartbeat")
            if isinstance(heartbeat, dict):
                heartbeat.pop("model", None)

        # utilityModel and subagents.model are provider-specific refs written
        # at provision time (titles/utility routing + the spawn-tier pin) —
        # re-pin or delete them so no stale ref survives the swap. subagents
        # siblings (allowAgents, runTimeoutSeconds) are provider-independent
        # and preserved.
        if utility_model is not None:
            defaults["utilityModel"] = utility_model
        else:
            defaults.pop("utilityModel", None)
        if subagent_model is not None:
            subagents = defaults.get("subagents")
            if not isinstance(subagents, dict):
                subagents = {}
                defaults["subagents"] = subagents
            subagents["model"] = subagent_model
        else:
            subagents = defaults.get("subagents")
            if isinstance(subagents, dict):
                subagents.pop("model", None)

        # tools.web.search.provider: re-pin when a provider is given, delete
        # when None — same shape as the model overrides above. Only the search
        # subtree is touched; fetch config survives. Callers currently always
        # pass parallel-free (see billing_service): the None/delete branch is
        # the generic clear, not a live provider path.
        tools = current.get("tools")
        if web_search_provider is not None:
            # Create the path when absent (parity with the heartbeat/subagents
            # handling above) — a hand-edited or ancient config missing `tools`
            # must still come out of the swap with a working web_search.
            if not isinstance(tools, dict):
                tools = {}
                current["tools"] = tools
            web = tools.get("web")
            if not isinstance(web, dict):
                web = {}
                tools["web"] = web
            search = web.get("search")
            if not isinstance(search, dict):
                search = {}
                web["search"] = search
            search["provider"] = web_search_provider
        elif isinstance(tools, dict):
            web = tools.get("web")
            if isinstance(web, dict):
                search = web.get("search")
                if isinstance(search, dict):
                    search.pop("provider", None)
                    if not search:
                        web.pop("search", None)

        # tools.toolSearch is written unconditionally at provision time, for every
        # provider choice — so callers should pass a mode to (re)pin it and the
        # swap keeps matching a fresh provision. None still deletes the key, kept
        # for callers that genuinely want it gone; it is no longer what a provider
        # swap wants, since stripping it drops the owner back to carrying the full
        # tool catalog.
        tools = current.get("tools")
        if tool_search_mode is not None:
            if not isinstance(tools, dict):
                tools = {}
                current["tools"] = tools
            tools["toolSearch"] = {"mode": tool_search_mode}
        elif isinstance(tools, dict):
            tools.pop("toolSearch", None)

        # Keep the CEO (Friday) per-agent model override in sync with the new
        # provider. The CEO is the roster entry with default: true. When
        # ceo_model is set, pin it; when None (flat-rate provider with no
        # premium tier), drop any stale override so Friday inherits the new
        # default — otherwise a Bedrock -> ChatGPT switch leaves Friday pointing
        # at a provider no longer in models.providers.
        #
        # Roster-shape aware (see core/services/agent_roster.py): reading only
        # agents.list makes this a silent no-op on any 8.1 container, whose file
        # boot doctor has already migrated to agents.entries. In the keyed shape
        # read_agent_roster hands back COPIES, so the mutated roster has to be
        # written back rather than mutated in place.
        roster_shape, roster = read_agent_roster(agents)
        ceo_entry = next(
            (e for e in roster if isinstance(e, dict) and e.get("default") is True),
            None,
        )
        if ceo_entry is not None:
            if ceo_model is not None:
                ceo_entry["model"] = ceo_model
            else:
                ceo_entry.pop("model", None)
            write_agent_roster(agents, roster_shape, roster)

        plugins = current.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            current["plugins"] = plugins
        # Provider entries are replaced wholesale (to drop the OLD provider's
        # entry on a switch), but the provider-independent bundled extensions
        # must persist — otherwise a Bedrock ↔ ChatGPT swap would drop
        # lobster / llm-task (workflow plugins) and memory-core (dreaming)
        # until full reprovision. Re-seed them from the same sources the
        # provision builder uses. Distinct keys, so the provider entries
        # below never collide with them.
        plugins["entries"] = {
            **workflow_plugin_entries(llm_task_model=llm_task_model),
            **memory_plugin_entries(owner_id),
            **copy.deepcopy(acpx_entry or {}),
            **copy.deepcopy(plugin_entries),
        }

        # The harness set follows the provider, so the swap replaces the root
        # acp block wholesale (a deep-merge could not clear the old
        # allowedAgents). The acpx plugin entry re-seeds via the rebuild
        # above.
        if acp_block is not None:
            current["acp"] = copy.deepcopy(acp_block)

        # memorySearch is intentionally NOT rewritten here. Its embedding
        # backend is provider-derived (config.memory_search_entry), but both
        # providers this hot path ever swaps between — bedrock_claude and
        # chatgpt_oauth — resolve to the same FTS-only value (provider:"none"),
        # so there is nothing to change. The only path whose memorySearch
        # differs (byo_key+openai -> provider:"openai") always takes the cold
        # re-provision path, which rebuilds the block from scratch. Recall
        # stays on regardless.

        return True

    await _locked_rmw(owner_id, _mutate, "replace provider block")


async def patch_openclaw_config(owner_id: str, patch: dict, *, agent_for_account: dict[str, str] | None = None) -> None:
    """Patch openclaw.json on EFS with file locking and atomic write.

    Deep-merges the patch into the existing config and writes the result
    atomically. Raises ConfigPatchError if the config file doesn't exist.
    Always writes (even for empty patches) to preserve historical behavior.

    Channel invariant: every ``channels.<provider>.accounts.<account_id>``
    introduced by the patch also gets a matching top-level ``bindings``
    entry in the same atomic write. Required by OpenClaw's inbound-message
    dispatcher (desktop/openclaw src/routing/bindings.ts) or the bot
    misroutes to the default agent (``main``). ``agent_for_account`` — see
    ``_ensure_channel_bindings_for_patch`` — routes an account to a specific
    agent instead of the legacy ``agentId == accountId`` default.
    """

    def _mutate(current: dict) -> bool:
        merged = _deep_merge(current, patch)
        current.clear()
        current.update(merged)
        # Apply within the same locked RMW so account block + route binding
        # land atomically — no window where one exists without the other.
        _ensure_channel_bindings_for_patch(current, patch, agent_for_account)
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
    - Acquires the same OpenClaw-compatible sidecar as
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
    - Acquires the same OpenClaw-compatible sidecar as
      append_to_openclaw_config_list.
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


async def apply_deploy_aux_mutations(
    owner_id: str,
    *,
    plugins_patch: dict,
    mcp_patch: dict | None = None,
    skills_patch: dict | None = None,
    agent_entry: dict | None = None,
) -> None:
    """Atomically apply the auxiliary subtree mutations for a catalog
    deploy to openclaw.json.

    Marketplace deploys land state via TWO paths:

    1. The RPC path (``pair_with_openclaw`` from ``catalog_service``)
       updates the OpenClaw container's in-memory agent state right
       away — so the buyer's sidebar shows the right friendly name
       without waiting for chokidar. OpenClaw's ``agents.update`` AJV
       schema is narrow (``additionalProperties: false`` — only
       ``agentId, name, workspace, model, emoji, avatar``), so the RPC
       can only carry display-level fields.

    2. This file-merge path (you are here) handles the rest of the
       agent entry — ``skills`` binding, ``reasoningDefault``,
       ``agentRuntime``, ``embeddedHarness``, etc. — plus the
       top-level ``plugins`` / ``mcp.servers`` / ``skills.entries``
       blocks. Chokidar polls EFS (1-30s) and reloads, after which
       the in-memory agent picks up the full per-agent config.

    Mutations applied under one exclusive lock so concurrent deploys
    cannot race on the read-modify-write of the touched subtrees:

      1. (Optional) Append ``agent_entry`` to ``config["agents"]["list"]``,
         creating ``agents.list`` if absent. If an entry with the same
         ``id`` already exists (idempotent retry), it is replaced
         in place rather than duplicated.
      2. Deep-merge ``plugins_patch`` into ``config["plugins"]``.
      3. (Optional) Deep-merge ``mcp_patch`` (e.g. ``{"servers": {…}}``)
         into ``config["mcp"]``.
      4. (Optional) Deep-merge ``skills_patch.entries`` and union
         ``skills_patch.allowBundled`` into ``config["skills"]``.

    NOTE: top-level ``tools`` is intentionally left alone. The catalog
    code used to write ``tools.allowed = []``, which is not a valid key
    in OpenClaw's ``ToolsSchema`` and caused chokidar's hot-reload to
    reject the entire config. Self-heals: if a stale ``tools.allowed``
    is present from before this fix, drop it on the next mutation so
    the file becomes valid again.
    """

    def _mutate(current: dict) -> bool:
        if agent_entry is not None:
            agents_obj = current.get("agents")
            # Tolerate the legacy flat-list shape on read; normalize to
            # the canonical ``{list: [...]}`` shape on write.
            if isinstance(agents_obj, list):
                agents_obj = {"list": list(agents_obj)}
            elif not isinstance(agents_obj, dict):
                agents_obj = {}
            roster_shape, agents_list = read_agent_roster(agents_obj)
            new_id = agent_entry.get("id")
            existing_idx = next(
                (i for i, a in enumerate(agents_list) if isinstance(a, dict) and a.get("id") == new_id),
                None,
            )
            entry_copy = copy.deepcopy(agent_entry)
            if existing_idx is not None:
                agents_list[existing_idx] = entry_copy
            else:
                agents_list.append(entry_copy)
            write_agent_roster(agents_obj, roster_shape, agents_list)
            current["agents"] = agents_obj

        if plugins_patch:
            existing_plugins = current.get("plugins")
            if not isinstance(existing_plugins, dict):
                existing_plugins = {}
            current["plugins"] = _deep_merge(existing_plugins, plugins_patch)

        if mcp_patch:
            existing_mcp = current.get("mcp")
            if not isinstance(existing_mcp, dict):
                existing_mcp = {}
            current["mcp"] = _deep_merge(existing_mcp, mcp_patch)

        if skills_patch:
            existing_skills = current.get("skills")
            if not isinstance(existing_skills, dict):
                existing_skills = {}

            patch_bundled = skills_patch.get("allowBundled")
            patch_minus_bundled = {k: v for k, v in skills_patch.items() if k != "allowBundled"}
            if patch_minus_bundled:
                existing_skills = _deep_merge(existing_skills, patch_minus_bundled)
            if isinstance(patch_bundled, list):
                existing_bundled = existing_skills.get("allowBundled")
                if not isinstance(existing_bundled, list):
                    existing_bundled = []
                merged_bundled = list(existing_bundled)
                for name in patch_bundled:
                    if name not in merged_bundled:
                        merged_bundled.append(name)
                existing_skills["allowBundled"] = merged_bundled
            current["skills"] = existing_skills

        # Self-heal: strip the invalid ``tools.allowed`` key if present
        # (left over from pre-fix deploys). Cleans up the bad on-disk state
        # so OpenClaw's strict-schema validator accepts the file again.
        tools = current.get("tools")
        if isinstance(tools, dict) and "allowed" in tools:
            del tools["allowed"]

        return True

    await _locked_rmw(
        owner_id,
        _mutate,
        f"deploy aux merge agent_entry={bool(agent_entry)} plugins={bool(plugins_patch)} "
        f"mcp={bool(mcp_patch)} skills={bool(skills_patch)}",
        # The only writer merging UNTRUSTED input: these subtrees come from a
        # marketplace seller's openclaw-slice.json. A slice that would not load
        # is rejected rather than audited — the buyer loses a deploy, not their
        # container at its next restart.
        enforce_schema=True,
    )


async def apply_paperclip_meta(
    owner_id: str,
    *,
    agent_id: str,
    meta: dict,
) -> None:
    """Deep-merge ``params.paperclip`` into ``agents.list[idx]`` for the
    matching ``agent_id`` in the owner's ``openclaw.json``.

    OpenClaw's on-disk ``AgentEntrySchema`` accepts arbitrary
    ``params`` (``z.record(z.string(), z.unknown())``) but its
    ``agents.update`` RPC schema rejects it. So we write the
    meta directly to the file via the existing locked RMW path —
    same mechanism ``apply_deploy_aux_mutations`` uses for plugins /
    mcp / skills writes.

    The data is purely a carrier for marketplace publish slice
    extraction; nothing inside OpenClaw consumes
    ``params.paperclip`` at runtime, so chokidar reload lag is
    irrelevant.

    ``meta`` is the partial paperclip block, e.g.
    ``{"role": "engineer", "title": "Backend dev"}``. Existing values
    for unspecified keys are preserved (deep-merge semantics; matches
    ``apply_deploy_aux_mutations``'s plugins-merge contract).

    Empty-string sentinel = "clear this field". Matches the
    ``AgentOverviewTab`` UI contract: the form always sends
    ``title``/``capabilities``/``icon`` as trimmed strings — an empty
    string means the user cleared the field, and the deep-merge would
    otherwise leave the old value in place. After merging we sweep
    these three keys and drop any whose value is a whitespace-only
    string, so the on-disk block never carries empty sentinels.

    No-op if the agent isn't in ``agents.list`` (the user might have
    deleted it concurrently — surface as a soft failure rather than
    a write to a phantom entry).
    """

    def _mutate(current: dict) -> bool:
        agents_obj = current.get("agents")
        if isinstance(agents_obj, list):
            # Legacy flat-list tolerance (matches apply_deploy_aux_mutations).
            agents_list = list(agents_obj)
            agents_obj = {"list": agents_list}
            current["agents"] = agents_obj
            roster_shape = "list"
        elif isinstance(agents_obj, dict):
            roster_shape, agents_list = read_agent_roster(agents_obj)
            if not agents_list:
                return False
        else:
            return False

        for idx, entry in enumerate(agents_list):
            if not isinstance(entry, dict):
                continue
            if entry.get("id") != agent_id:
                continue
            params = entry.get("params")
            if not isinstance(params, dict):
                params = {}
                entry["params"] = params
            existing_pc = params.get("paperclip")
            if not isinstance(existing_pc, dict):
                existing_pc = {}
            existing_pc.update(meta)
            # Empty-string sentinel means "clear this field" — match
            # the frontend's contract in AgentOverviewTab. Without
            # this sweep, the deep-merge would leave the old value
            # in place and the user can't actually remove a
            # once-set title/capabilities/icon.
            for k in ("title", "capabilities", "icon"):
                v = existing_pc.get(k)
                if isinstance(v, str) and not v.strip():
                    existing_pc.pop(k, None)
            params["paperclip"] = existing_pc
            agents_list[idx] = entry
            write_agent_roster(agents_obj, roster_shape, agents_list)
            return True

        # agent_id not found — no-op write, return False so the
        # _locked_rmw helper skips the file write entirely.
        return False

    await _locked_rmw(
        owner_id,
        _mutate,
        f"paperclip-meta merge agent_id={agent_id!r}",
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
    - Acquires the same OpenClaw-compatible sidecar as the other helpers.
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


# Provider-level credential keys OpenClaw reads as a synthetic "[default]"
# account. The setup wizard only ever writes tokens under accounts.<id>; any of
# these sitting directly on channels.<provider> is a legacy orphan that auths
# and crash-loops on revoked creds (auth.test -> invalid_auth, getMe -> 401).
PROVIDER_LEVEL_CHANNEL_SECRET_KEYS = ("botToken", "appToken", "token", "signingSecret")


async def delete_provider_level_channel_secrets(owner_id: str, provider: str) -> None:
    """Strip legacy provider-level credential keys from ``channels.<provider>``.

    Removes ``botToken`` / ``appToken`` / ``token`` / ``signingSecret`` that sit
    directly on the channel object (NOT under ``accounts.<id>``). Leaves
    ``accounts``, ``enabled``, ``dmPolicy``, ``mode`` and everything else intact
    — an enabled channel with no account and no provider-level secret is silent.

    No-op (no write, no ``.bak``) when none of the keys are present.
    """

    def _mutate(current: dict) -> bool:
        channels = current.get("channels")
        if not isinstance(channels, dict):
            return False
        channel = channels.get(provider)
        if not isinstance(channel, dict):
            return False
        changed = False
        for key in PROVIDER_LEVEL_CHANNEL_SECRET_KEYS:
            if key in channel:
                del channel[key]
                changed = True
        return changed

    await _locked_rmw(owner_id, _mutate, f"strip provider-level secrets channels.{provider}")


async def strip_removed_agent_keys(
    owner_id: str,
    *,
    defaults_keys: Iterable[str],
    entry_keys: Iterable[str],
) -> None:
    """Remove deprecated keys from ``agents.defaults`` and every
    ``agents.list[]`` entry in the owner's openclaw.json.

    For migrating an existing on-disk config before a container is rolled to
    a newer image whose strict schema rejects keys an older image (or an
    older marketplace deploy) wrote. Idempotent — a no-op when none of the
    keys are present. Only top-level entry keys are stripped; nested
    structures (e.g. ``models[*].agentRuntime``) are left intact.
    """
    defaults_set = frozenset(defaults_keys)
    entry_set = frozenset(entry_keys)

    def _mutate(current: dict) -> bool:
        agents = current.get("agents")
        if not isinstance(agents, dict):
            return False
        changed = False
        defaults = agents.get("defaults")
        if isinstance(defaults, dict):
            for key in defaults_set:
                if key in defaults:
                    del defaults[key]
                    changed = True
        # Roster-shape aware: after boot doctor migrates the file to
        # agents.entries, reading only agents.list makes this scrubber a SILENT
        # NO-OP -- and this function's whole job is to clear keys a newer strict
        # schema rejects before the container is rolled onto it. Failing quietly
        # here means the roll it was meant to protect fails instead.
        roster_shape, roster = read_agent_roster(agents)
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            for key in entry_set:
                if key in entry:
                    del entry[key]
                    changed = True
        if changed and roster:
            write_agent_roster(agents, roster_shape, roster)
        return changed

    await _locked_rmw(owner_id, _mutate, "strip removed agent keys")


async def ensure_plugin_entries(owner_id: str, entries: dict) -> None:
    """Ensure ``plugins.entries`` contains each of ``entries``.

    ADDITIVE ONLY — adds entries that are absent, never overwrites one the
    owner already has. A plugin the owner (or a marketplace deploy) configured
    is theirs; this only fills gaps.

    Exists because nothing else puts a NEW plugin entry on an existing
    container: the image roll bakes the extension but reuses the on-disk
    config, and ``replace_provider_block`` rebuilds the plugin subtree only on
    a provider swap. Without this a baked plugin never activates for anyone
    provisioned before it shipped.
    """
    wanted = dict(entries)

    def _mutate(current: dict) -> bool:
        plugins = current.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            current["plugins"] = plugins
        existing = plugins.get("entries")
        if not isinstance(existing, dict):
            existing = {}
            plugins["entries"] = existing
        changed = False
        for key, value in wanted.items():
            if key not in existing:
                existing[key] = value
                changed = True
        return changed

    await _locked_rmw(owner_id, _mutate, f"ensure plugin entries={sorted(wanted)}")


async def ensure_plugin_config_keys(owner_id: str, plugin: str, keys: dict) -> None:
    """Ensure ``plugins.entries.<plugin>.config`` contains each of ``keys``.

    NESTED-ADDITIVE — deep-fills any key path that is ABSENT and never
    overwrites one that is present. Dict values recurse, so a half-configured
    subtree gains only its missing leaves; scalars and lists are written only
    when the key is missing outright. A present value is left alone so this
    can never clobber a key some other writer owns; it is NOT an opt-out
    mechanism — the roll's regen path (``reapply_owner_config``) rewrites
    ``plugins`` wholesale from the generator first, and this runs after it
    (a no-op there) and on the legacy fallback path that regen cannot reach.

    Does nothing when the plugin entry itself is absent — that is
    ``ensure_plugin_entries``' job, which runs first on the same path.

    Exists because ``ensure_plugin_entries`` is additive at the ENTRY level: it
    skips any plugin key already present, so it cannot reach INSIDE an entry
    the owner already has. An image roll bakes new plugin behaviour but reuses
    the on-disk config, so a config key we start emitting at provision time
    would otherwise reach only owners provisioned after it shipped — every
    existing owner would carry the enabled plugin without the keys that make it
    do the new thing. Idempotent, so it is safe on every roll.
    """
    wanted = copy.deepcopy(keys)

    def _fill(target: dict, patch: dict) -> bool:
        changed = False
        for key, value in patch.items():
            if key not in target:
                target[key] = copy.deepcopy(value)
                changed = True
            elif isinstance(value, dict) and isinstance(target[key], dict):
                changed = _fill(target[key], value) or changed
        return changed

    def _mutate(current: dict) -> bool:
        if not wanted:
            return False
        plugins = current.get("plugins")
        entries = plugins.get("entries") if isinstance(plugins, dict) else None
        entry = entries.get(plugin) if isinstance(entries, dict) else None
        if not isinstance(entry, dict):
            return False
        config = entry.setdefault("config", {})
        if not isinstance(config, dict):
            return False
        return _fill(config, wanted)

    await _locked_rmw(owner_id, _mutate, f"ensure plugin config plugin={plugin} keys={sorted(wanted)}")


async def ensure_redact_patterns(owner_id: str, patterns: Iterable[str]) -> None:
    """Ensure ``logging.redactPatterns`` contains each of ``patterns``.

    ADDITIVE ONLY — appends what is missing, never removes or reorders. The
    owner (or a future generator) may have patterns of their own, and upstream
    prepends whatever is here to its own defaults, so nothing is lost either
    way.

    Exists because a redaction rule is worthless if it only reaches owners
    provisioned after it shipped. Nothing else carries new config keys onto an
    existing container: the image roll only STRIPS removed keys
    (``migrate_removed_config_keys``) and heals seeded files, and
    ``replace_provider_block`` rebuilds the provider/plugin subtrees but never
    ``logging``. So an existing owner would keep the playbook that reads their
    Paperclip key while never gaining the pattern that redacts it — the one
    cohort that needs it most.

    Safe to run on anyone: adding a redaction pattern cannot change agent
    behaviour, only what reaches the transcript. Idempotent.
    """
    wanted = list(patterns)

    def _mutate(current: dict) -> bool:
        logging_cfg = current.get("logging")
        if not isinstance(logging_cfg, dict):
            logging_cfg = {}
            current["logging"] = logging_cfg
        existing = logging_cfg.get("redactPatterns")
        if not isinstance(existing, list):
            existing = []
        missing = [p for p in wanted if p not in existing]
        if not missing:
            return False
        logging_cfg["redactPatterns"] = existing + missing
        return True

    await _locked_rmw(owner_id, _mutate, "ensure redact patterns")


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
    - Same OpenClaw-compatible sidecar as the other helpers.

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
    """Atomically upsert cron jobs into ``{owner_id}/cron/jobs.json``.

    Lives in a separate file from openclaw.json (see ``config.cron.store``
    in OpenClaw's schema, default ``~/.openclaw/cron/jobs.json``), so it
    needs its own ``fcntl.lockf`` rather than reusing the openclaw.json
    lock. Same atomic-write pattern as ``_locked_rmw``: open the data
    file, take an exclusive lock, read + modify, tempfile-rename, chown.

    No-op if ``new_jobs`` is empty.

    Uses the same shared two-layer lock as ``_locked_rmw`` (see
    ``efs_file_lock``): a per-file in-process ``threading.Lock`` plus an
    exclusive ``fcntl.lockf`` on a SEPARATE ``jobs.json.lock`` file — never
    jobs.json itself, whose inode the atomic ``os.rename`` swaps out.
    """
    if not new_jobs:
        return

    cron_dir = os.path.join(_efs_mount_path, owner_id, "cron")
    jobs_path = os.path.join(cron_dir, "jobs.json")
    lock_path = os.path.join(cron_dir, "jobs.json.lock")

    def _do():
        os.makedirs(cron_dir, exist_ok=True)
        chown_uid_1000_best_effort(cron_dir)
        if not os.path.exists(jobs_path):
            with open(jobs_path, "w") as f:
                json.dump({"version": 1, "jobs": []}, f)
            chown_uid_1000_best_effort(jobs_path)

        lock_fd = open_lock_fd(cron_dir, lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            with open(jobs_path, "r") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    data = {"version": 1, "jobs": []}
            if not isinstance(data, dict):
                data = {"version": 1, "jobs": []}
            jobs = data.get("jobs")
            if not isinstance(jobs, list):
                jobs = []
            existing_by_id = {
                job.get("id"): index
                for index, job in enumerate(jobs)
                if isinstance(job, dict) and isinstance(job.get("id"), str)
            }
            for new_job in new_jobs:
                copied = copy.deepcopy(new_job)
                job_id = copied.get("id") if isinstance(copied, dict) else None
                if isinstance(job_id, str) and job_id in existing_by_id:
                    jobs[existing_by_id[job_id]] = copied
                else:
                    if isinstance(job_id, str):
                        existing_by_id[job_id] = len(jobs)
                    jobs.append(copied)
            data["jobs"] = jobs
            data.setdefault("version", 1)

            json.dumps(data)  # validate serializability before writing

            fd, tmp_path = tempfile.mkstemp(dir=cron_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                chown_uid_1000_best_effort(tmp_path)
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

    def _do_locked():
        with get_file_lock(lock_path):
            _do()

    await asyncio.to_thread(_do_locked)


async def remove_cron_jobs(owner_id: str, job_ids: list[str]) -> None:
    """Atomically remove cron jobs by id from ``{owner_id}/cron/jobs.json``.

    The unwind side of :func:`append_cron_jobs` — a failed deploy must take
    its appended jobs with it, or the stranded jobs keep firing against an
    agent that no longer exists (and a retry appends duplicates beside them).
    Same two-layer lock + tempfile-rename pattern as the append. No-op when
    ``job_ids`` is empty or the file doesn't exist.
    """
    if not job_ids:
        return
    ids = set(job_ids)

    cron_dir = os.path.join(_efs_mount_path, owner_id, "cron")
    jobs_path = os.path.join(cron_dir, "jobs.json")
    lock_path = os.path.join(cron_dir, "jobs.json.lock")

    def _do():
        if not os.path.exists(jobs_path):
            return

        lock_fd = open_lock_fd(cron_dir, lock_path)
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_EX)
            with open(jobs_path, "r") as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError:
                    return
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                return
            jobs = data["jobs"]
            kept = [j for j in jobs if not (isinstance(j, dict) and j.get("id") in ids)]
            if len(kept) == len(jobs):
                return
            data["jobs"] = kept

            fd, tmp_path = tempfile.mkstemp(dir=cron_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f, indent=2)
                chown_uid_1000_best_effort(tmp_path)
                os.rename(tmp_path, jobs_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise

            logger.info(
                "cron/jobs.json for owner %s: removed %d job(s)",
                owner_id,
                len(jobs) - len(kept),
            )
        finally:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

    def _do_locked():
        with get_file_lock(lock_path):
            _do()

    await asyncio.to_thread(_do_locked)
