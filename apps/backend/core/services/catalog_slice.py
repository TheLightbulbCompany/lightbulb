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

_STRIPPED_KEYS = frozenset({"model", "channels", "workspace", "id", "agentDir"})

# Secret-shaped field names scrubbed from slice copies of mcp.servers[*] /
# skills.entries[*]. Values are replaced with empty strings (key names
# preserved so the buyer sees what to fill in on their side). Mirrors
# upstream OpenClaw's zod ``z.sensitive()`` markers — see
# ``openclaw/src/config/zod-schema.ts`` (apiKey, env, headers,
# webhookToken). The publisher's values never leave their container.
_SECRET_FIELD_NAMES = frozenset({"apiKey", "env", "headers", "webhookToken"})


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
    return result


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
# (see openclaw/src/config/agent-dirs.ts:60-79).


def strip_user_specific_fields(agent_entry: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(agent_entry)
    for key in _STRIPPED_KEYS:
        result.pop(key, None)
    return result


def _agents_list(openclaw_json: dict[str, Any]) -> list[Any]:
    """Return the agent-entry array from an openclaw.json.

    OpenClaw's config schema (zod: ``openclaw/src/config/zod-schema.agents.ts``):

        agents: { defaults: AgentDefaults, list: AgentEntry[] }

    Early versions of the Isol8 catalog code read ``config["agents"]`` as if it
    were a flat list — that worked only for configs that didn't exist, which
    is why it went unnoticed until the first real publish attempt. The write
    path (``config_patcher.apply_deploy_mutation``) migrates flat-list configs
    to the nested shape on deploy; this reader accepts BOTH so an admin with a
    legacy config can still publish without first triggering a migration.
    """
    agents = openclaw_json.get("agents")
    if isinstance(agents, list):
        return agents  # legacy flat shape — tolerated for read/publish
    if not isinstance(agents, dict):
        return []
    lst = agents.get("list")
    return lst if isinstance(lst, list) else []


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

    slice_: dict[str, Any] = {
        "agent": strip_user_specific_fields(agent_entry),
        "plugins": copy.deepcopy(openclaw_json.get("plugins") or {}),
        "tools": copy.deepcopy(openclaw_json.get("tools") or {}),
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
