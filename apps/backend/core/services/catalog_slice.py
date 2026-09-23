"""Pure functions for slicing an agent's entry out of an openclaw.json and
stripping user/tier-specific fields that must not leak into the catalog.

User/tier-specific fields never go in a catalog package:
  - model (user's tier picks a default at runtime)
  - channels (per-user credentials)
  - workspace (path; the deploy generates a new one)
  - id (regenerated per-deploy)

Behavioral fields stay:
  - skills list, plugins config, tools allowlist, cron, thinkingDefault, etc.
"""

from __future__ import annotations

import copy
import enum
from collections.abc import Iterable
from typing import Any

from core.services.agent_roster import roster_entries

_STRIPPED_KEYS = frozenset({"model", "channels", "workspace", "id", "agentDir"})

# Top-level agent-entry keys that upstream removed from the strict
# AgentEntrySchema. A seller's pre-upgrade config (and any already-published
# immutable artifact) may still carry one; deep-merging it into a buyer's
# openclaw.json makes the next strict reload fail and crash-loops the
# container. Consumed by update_service's config reconciler, which sweeps
# retired keys out of EXISTING on-disk configs on image upgrades. The
# publish/deploy paths use the ALLOWED whitelist below instead — a blacklist
# can only name the keys we already know about.
REMOVED_AGENT_ENTRY_KEYS = frozenset({"embeddedHarness", "embeddedPi", "agentRuntime", "systemPromptOverride"})

# The strict AgentEntrySchema's complete top-level key set — OpenClaw's
# ``agents.list[]`` entries are ``z.object({...}).strict()``, so ONE key
# outside this set makes the whole config fail validation and takes the
# owner's gateway down until the config is repaired. Sources of agent entries
# are not trusted to be schema-shaped: an Atlas-built staging config invented
# ``archetype`` (bricking the founders' container on the first live
# register), and a seller export can carry anything its author
# typed. Whitelist, don't blacklist.
#
# Derived from the deployed fork tag's ``src/config/zod-schema.agent-runtime.ts``
# ``AgentEntrySchema`` (verified byte-identical to fork main, 2026-07-31).
# When the schema gains a key, add it here — until then a new key is stripped
# at deploy, which degrades one field instead of bricking the container.
ALLOWED_AGENT_ENTRY_KEYS = frozenset(
    {
        "id",
        "default",
        "name",
        "description",
        "workspace",
        "agentDir",
        "model",
        "utilityModel",
        "models",
        "thinkingDefault",
        "verboseDefault",
        "toolProgressDetail",
        "reasoningDefault",
        "fastModeDefault",
        "contextInjection",
        "bootstrapMaxChars",
        "bootstrapTotalMaxChars",
        "experimental",
        "skills",
        "memorySearch",
        "humanDelay",
        "tts",
        "skillsLimits",
        "contextLimits",
        "contextTokens",
        "heartbeat",
        "identity",
        "groupChat",
        "subagents",
        "runRetries",
        "embeddedAgent",
        "sandbox",
        "params",
        "tools",
        "runtime",
    }
)

# Entry keys 2026.8.1 removed from AgentEntrySchema. The whitelist above is
# derived from the 7.2 schema, so without this a slice extracted from a 7.2-era
# container (a marketplace republish, or a backup import) carries them through
# and apply_deploy_aux_mutations -- which gates untrusted slices with
# enforce_schema=True -- rejects the whole deploy on an 8.1 container.
#
# Verified against the vendored 8.1 schema rather than assumed: `models` is
# STILL VALID on an entry at 8.1 (AgentModelRuntimeEntrySchema) and must not be
# dropped; only these three are rejected.
_REMOVED_AT_2026_8_1 = frozenset({"runRetries", "memorySearch", "contextTokens"})


def _allowed_agent_entry_keys() -> frozenset[str]:
    """Whitelist for an agent entry in a marketplace slice.

    The 2026.8.1 image gate is gone with the image pin it read: every key
    ``_REMOVED_AT_2026_8_1`` names is dropped unconditionally now.
    """
    return ALLOWED_AGENT_ENTRY_KEYS - _REMOVED_AT_2026_8_1


# Provider plugins are user-config (the buyer picked a provider at signup),
# not agent-config. Shipping them in the slice would deep-merge over the
# buyer's provider setup and silently switch them off the path they chose
# (Bedrock / ChatGPT OAuth / BYOK Anthropic / BYOK OpenAI). Strip at slice
# time so the deploy can never override the buyer's provider plugin.
_PROVIDER_PLUGIN_NAMES = frozenset(
    {
        "amazon-bedrock",
        "chatgpt",
        "chatgpt-oauth",
        "anthropic",
        "openai",
        "google",
        "gemini",
        "ollama",
        "mistral",
    }
)


def _strip_provider_plugins(plugins: dict[str, Any]) -> dict[str, Any]:
    """Drop provider entries from a plugins block, leaving structural keys
    (``slots``, anything non-``entries``) and non-provider entries intact."""
    out = copy.deepcopy(plugins) if plugins else {}
    entries = out.get("entries") if isinstance(out, dict) else None
    if isinstance(entries, dict):
        out["entries"] = {k: v for k, v in entries.items() if k not in _PROVIDER_PLUGIN_NAMES}
    return out


def _scrub_plugins_block(plugins: dict[str, Any]) -> dict[str, Any]:
    """Provider entries dropped, then every surviving entry scrubbed the same
    way as ``mcp.servers`` / ``skills.entries`` — plugin configs carry the
    same apiKey/env-shaped fields, and an unscrubbed one ships the seller's
    credential in the published slice."""
    out = _strip_provider_plugins(plugins)
    entries = out.get("entries") if isinstance(out, dict) else None
    if isinstance(entries, dict):
        out["entries"] = {
            name: _scrub_secret_fields(entry) if isinstance(entry, dict) else entry for name, entry in entries.items()
        }
    return out


# Secret-shaped field names scrubbed from slice copies of mcp.servers[*] /
# skills.entries[*]. Values are replaced with empty strings (key names
# preserved so the buyer sees what to fill in on their side). Mirrors
# upstream OpenClaw's zod ``z.sensitive()`` markers — see
# ``openclaw/src/config/zod-schema.ts`` (apiKey, env, headers,
# webhookToken). The publisher's values never leave their container.
_SECRET_FIELD_NAMES = frozenset({"apiKey", "env", "headers", "webhookToken"})

# Secret CLI flags whose following value (or ``=``-joined value) is a credential.
# MCP servers commonly launch via ``npx -y <pkg> --api-key <KEY>``; the field-name
# scrub never sees these because the secret sits positionally in ``args``.
_SECRET_ARG_FLAGS = frozenset({"--api-key", "--apikey", "--key", "--token", "--secret", "--password"})


def _scrub_arg_secrets(args: list[Any]) -> list[Any]:
    """Copy of ``args`` with the value of any secret flag blanked.

    Separated ``["--api-key", "<VAL>"]`` -> ``"<VAL>"`` becomes ``""``.
    Joined ``["--api-key=<VAL>"]`` -> ``"--api-key="``. Flag tokens are kept so
    the buyer sees which credential to supply. Non-string elements pass through.
    """
    out: list[Any] = []
    blank_next = False
    for a in args:
        if blank_next:
            out.append("" if isinstance(a, str) else a)
            blank_next = False
            continue
        if isinstance(a, str):
            if a.lower() in _SECRET_ARG_FLAGS:
                out.append(a)
                blank_next = True
                continue
            eq = a.find("=")
            if eq != -1 and a[:eq].lower() in _SECRET_ARG_FLAGS:
                out.append(a[: eq + 1])
                continue
        out.append(a)
    return out


class SkillBucket(str, enum.Enum):
    """Where a skill lives on the seller's container.

    Drives publish-time slice synthesis and buyer-side deploy:
      - ``BUNDLED``  → name baked into the buyer's OpenClaw image already.
                      Slice carries it in ``skills.allowBundled``.
      - ``ENTRIES``  → name configured in ``skills.entries[name]`` with
                      apiKey/env. Slice copies the entry with secrets
                      scrubbed; deploy deep-merges into buyer's config.
      - ``WORKSPACE``→ skill source files live under the agent's workspace
                      and ship in the tarball directly. No openclaw.json
                      mutation needed.
      - ``EXTRA_DIRS``→ skill lives at a container-wide path outside the
                      agent workspace (``skills.load.extraDirs``). v1
                      surfaces a warning — auto-packaging deferred.
      - ``UNKNOWN``  → name not found anywhere on the seller's container.
                      The wizard surfaces this so the seller can correct
                      before publishing.
    """

    BUNDLED = "bundled"
    ENTRIES = "entries"
    WORKSPACE = "workspace"
    EXTRA_DIRS = "extra_dirs"
    UNKNOWN = "unknown"


def _scrub_secret_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``entry`` with every secret-shaped field value
    replaced by an empty string (or ``{k: ""}`` for dict-typed fields like
    ``env`` and ``headers``). Key names are preserved so the buyer sees
    which environment variables / headers the integration expects.

    Recurses one level: an MCP server's top-level ``env`` is scrubbed, but
    we don't deep-walk arbitrary nested structures — the upstream schema
    pins secret fields to the entry's top level.
    """
    result = copy.deepcopy(entry)
    for k in _SECRET_FIELD_NAMES:
        if k not in result:
            continue
        val = result[k]
        if isinstance(val, dict):
            result[k] = {kk: "" for kk in val.keys()}
        elif isinstance(val, list):
            result[k] = []
        else:
            result[k] = ""
    args = result.get("args")
    if isinstance(args, list):
        result["args"] = _scrub_arg_secrets(args)
    return result


def _scrub_tools_block(tools: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of the top-level ``tools`` block with every
    secret-shaped value blanked, anywhere in the structure.

    Unlike ``_scrub_secret_fields`` (which only inspects an entry's top
    level, matching upstream's flat MCP/skill secret schema), the ``tools``
    block is a free-form nested config (``tools.exec.env``,
    ``tools.fetch.headers``, ...). A future mistake placing a credential
    under any nested key would otherwise be deep-copied verbatim into the
    published artifact. We walk the whole tree and blank any key in
    ``_SECRET_FIELD_NAMES`` (dict → ``{kk: ""}``, list → ``[]``, else
    ``""``), preserving key names so the buyer sees what to fill in.
    """
    out = copy.deepcopy(tools)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if k in _SECRET_FIELD_NAMES:
                    if isinstance(v, dict):
                        node[k] = {kk: "" for kk in v}
                    elif isinstance(v, list):
                        node[k] = []
                    else:
                        node[k] = ""
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(out)
    return out


def classify_skill(
    name: str,
    openclaw_json: dict[str, Any],
    *,
    workspace_skill_names: Iterable[str] = (),
    extra_dir_skill_names: Iterable[str] = (),
) -> SkillBucket:
    """Classify a skill by where it lives on the seller's container.

    Resolution order (most specific first):
      1. ``skills.entries[name]``      → ``ENTRIES``  (config-registered)
      2. ``skills.allowBundled[*]``    → ``BUNDLED``  (image-baked)
      3. ``workspace_skill_names``     → ``WORKSPACE`` (ships in tarball)
      4. ``extra_dir_skill_names``     → ``EXTRA_DIRS`` (container-wide path)
      5. otherwise                     → ``UNKNOWN``

    ENTRIES wins over BUNDLED when a skill appears in both: a configured
    entry with secrets is more specific than just being on the
    allowBundled list.
    """
    skills_cfg = openclaw_json.get("skills") if isinstance(openclaw_json, dict) else None
    if not isinstance(skills_cfg, dict):
        skills_cfg = {}

    entries = skills_cfg.get("entries")
    if isinstance(entries, dict) and name in entries:
        return SkillBucket.ENTRIES

    allow_bundled = skills_cfg.get("allowBundled")
    if isinstance(allow_bundled, list) and name in allow_bundled:
        return SkillBucket.BUNDLED

    if name in set(workspace_skill_names):
        return SkillBucket.WORKSPACE

    if name in set(extra_dir_skill_names):
        return SkillBucket.EXTRA_DIRS

    return SkillBucket.UNKNOWN


# Cron-job fields stripped at slice time. Each is either runtime state (set
# by OpenClaw on first execution) or user-specific provenance regenerated
# at deploy. Schema reference: openclaw/src/config/types.cron.ts +
# the persisted shape in {owner_id}/cron/jobs.json.
_STRIPPED_CRON_KEYS = frozenset({"id", "sessionKey", "state", "createdAtMs", "updatedAtMs"})
# ``agentDir`` is per-agent-id (NOT per-user — the path is in-container
# uniform). Carrying the publisher's value into the slice means the deployed
# agent inherits a path keyed to the publisher's agent id, which:
#   - on self-deploy, collides with the publisher's existing agent and
#     trips OpenClaw's ``DuplicateAgentDirError`` (zod-rejected config →
#     hot-reload fails → new agent never appears in the running container);
#   - on cross-user deploy, dumps the new agent's data into a directory
#     keyed to the wrong id.
# Stripping it lets OpenClaw auto-derive ``/home/node/.openclaw/agents/{id}/agent``
# (see openclaw/src/config/agent-dirs.ts).


def strip_user_specific_fields(agent_entry: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(agent_entry)
    for key in _STRIPPED_KEYS:
        result.pop(key, None)
    return result


def sanitize_agent_entry_for_schema(agent_entry: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Drop top-level keys the strict AgentEntrySchema does not accept.

    Applied at deploy as well as publish: published artifacts are immutable,
    so an older slice already in S3 still carries whatever keys its author had
    (retired schema keys, or invented ones like Atlas's ``archetype``), and a
    single unrecognized key bricks the buyer's whole config on the next
    strict reload. Returns ``(sanitized_copy, dropped_keys)`` so callers can
    surface what was removed.
    """
    result = copy.deepcopy(agent_entry)
    dropped = sorted(k for k in result if k not in _allowed_agent_entry_keys())
    for key in dropped:
        result.pop(key, None)
    return result, dropped


def _agents_list(openclaw_json: dict[str, Any]) -> list[Any]:
    """Return the agent entries from an openclaw.json, in either roster shape.

    2026.8.1 keys the roster by agent id under ``agents.entries``; 7.2 (and
    every container that has not booted 8.1 yet) uses the ``agents.list``
    array. ``roster_entries`` reads both and re-injects the map key as ``id``,
    plus tolerates the legacy flat-list shape (``agents`` as a bare array)
    that hand-edited configs still carry — see
    ``core/services/agent_roster.py``.

    Reading only ``list`` here made every publish off an 8.1 container fail
    with "agent not found in openclaw.json".
    """
    return roster_entries(openclaw_json)


def filter_cron_jobs_for_agent(
    all_jobs: list[Any],
    agent_id: str,
) -> list[dict[str, Any]]:
    """Keep cron jobs whose ``agentId`` matches ``agent_id``; strip runtime +
    user-specific fields that must be regenerated at deploy.

    Persisted shape (one entry of ``jobs.json#/jobs``)::

        {
          "id":            <UUID>,         # regenerated on deploy
          "agentId":       <agent_id>,     # rewritten on deploy
          "sessionKey":    "agent:{id}:{userId}",  # regenerated on deploy
          "name":          ...,
          "description":   ...,
          "enabled":       bool,
          "schedule":      {kind, expr, tz},
          "sessionTarget": ...,
          "wakeMode":      ...,
          "payload":       {kind, message, ...},
          "delivery":      {mode, channel, accountId?},  # may reference a
                                                          # channel the deployer
                                                          # hasn't bound yet —
                                                          # carried as-is so
                                                          # the deployer can
                                                          # bind + flip the
                                                          # job on later
          "createdAtMs":   ...,            # regenerated on deploy
          "updatedAtMs":   ...,            # regenerated on deploy
          "state":         {nextRunAtMs,...} # OpenClaw recomputes from schedule
        }

    Non-dict entries are skipped silently (the persisted file has been
    observed with stray strings from hand-edits, same defensive posture as
    the agents list).
    """
    out: list[dict[str, Any]] = []
    for job in all_jobs:
        if not isinstance(job, dict):
            continue
        if job.get("agentId") != agent_id:
            continue
        clean = copy.deepcopy(job)
        for k in _STRIPPED_CRON_KEYS:
            clean.pop(k, None)
        out.append(clean)
    return out


def extract_agent_slice(
    openclaw_json: dict[str, Any],
    agent_id: str,
    *,
    selected_mcps: list[str] | None = None,
    selected_skills: list[str] | None = None,
    workspace_skill_names: Iterable[str] = (),
    extra_dir_skill_names: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a dict with the sliced agent entry plus the required plugins/tools
    from the publisher's config. Raises KeyError if the agent_id isn't present.

    When ``selected_mcps`` / ``selected_skills`` are provided (even as empty
    lists), the returned slice also carries:

      - ``mcp.servers``        — {name → scrubbed entry} for each selected MCP
      - ``skills.entries``     — {name → scrubbed entry} for each ENTRIES-bucket skill
      - ``skills.allowBundled``— list of BUNDLED-bucket skill names

    All secret-shaped fields (``apiKey``, ``env``, ``headers``,
    ``webhookToken``) are scrubbed to empty strings; the buyer's deploy
    deep-merges these into their openclaw.json and they fill in their
    own credentials.

    When the selections are ``None`` (the default), the slice is backwards-
    compatible: ``{agent, plugins, tools}`` — same shape the legacy deploy
    path expects.

    Also tolerates stray non-dict entries in ``agents.list`` (bare strings
    have been observed from hand-editing / partial runtime writes) — skip
    anything that isn't a dict rather than crashing with AttributeError.
    """
    agents = _agents_list(openclaw_json)
    matching = [a for a in agents if isinstance(a, dict) and a.get("id") == agent_id]
    if not matching:
        raise KeyError(f"agent {agent_id!r} not found in openclaw.json")
    agent_entry = matching[0]

    sanitized_agent, _dropped = sanitize_agent_entry_for_schema(strip_user_specific_fields(agent_entry))
    slice_: dict[str, Any] = {
        "agent": sanitized_agent,
        "plugins": _scrub_plugins_block(openclaw_json.get("plugins") or {}),
        "tools": _scrub_tools_block(openclaw_json.get("tools") or {}),
    }

    if selected_mcps is not None:
        mcp_servers_src = (openclaw_json.get("mcp") or {}).get("servers") or {}
        slice_["mcp"] = {
            "servers": {
                name: _scrub_secret_fields(mcp_servers_src[name])
                for name in selected_mcps
                if isinstance(mcp_servers_src.get(name), dict)
            }
        }

    if selected_skills is not None:
        skills_src = openclaw_json.get("skills") or {}
        entries_src = skills_src.get("entries") if isinstance(skills_src, dict) else None
        entries_src = entries_src if isinstance(entries_src, dict) else {}
        entries_out: dict[str, Any] = {}
        allow_bundled_out: list[str] = []
        for name in selected_skills:
            bucket = classify_skill(
                name,
                openclaw_json,
                workspace_skill_names=workspace_skill_names,
                extra_dir_skill_names=extra_dir_skill_names,
            )
            if bucket is SkillBucket.ENTRIES and isinstance(entries_src.get(name), dict):
                entries_out[name] = _scrub_secret_fields(entries_src[name])
            elif bucket is SkillBucket.BUNDLED:
                allow_bundled_out.append(name)
            # WORKSPACE: ships in the tarball, no slice mutation.
            # EXTRA_DIRS / UNKNOWN: surfaced by the wizard; not packaged in v1.
        slice_["skills"] = {
            "entries": entries_out,
            "allowBundled": allow_bundled_out,
        }

    return slice_
