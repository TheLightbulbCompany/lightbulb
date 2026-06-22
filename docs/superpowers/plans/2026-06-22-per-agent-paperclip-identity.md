# Per-Agent Paperclip Identity (plan)

2026-06-22 · status: APPROVED — implementing via subagents

## Problem

Every agent in a container authenticates to Paperclip as **Friday**. Two shared,
container-wide sources both carry Friday's key/agent_id: the **Paperclip MCP**
(`config.py` `mcp.servers.paperclip.env`, global + Friday-keyed + `runtimeScope:"shared"`
frozen) and the **static ECS task env** (`ecs_manager._paperclip_runtime_env`). So a worker
using the bundled `paperclip` skill's `curl` acts as Friday — wrong attribution, routines
created as Friday. We want true per-agent identity (Paperclip's native model).

## Why env/MCP can't fix it (research-established)

- OpenClaw exec env + MCP env are container-wide; MCP is global-only + shared-frozen. There
  is **no per-run env channel** (`PAPERCLIP_RUN_ID` never reaches exec or the MCP).
- The only per-agent/per-run identity channel that works is the **wake TEXT** the
  `openclaw_gateway` adapter sends (reaches the agent verbatim via the operator relay).
- Native Paperclip identity = a **per-agent claimed-key FILE** in the agent's own workspace
  + per-run wake text. `~/.openclaw/workspace` (singular) is container-GLOBAL (collides);
  per-agent workspaces are `~/.openclaw/workspaces/{agentId}` (plural) = the agent's cwd.

## Approved decisions

1. **Remove the Paperclip MCP** entirely (it can't be per-agent; leaving it lets workers act
   as Friday). All agents use the bundled skill (curl).
2. **Remove the static container env** (`_paperclip_runtime_env` + its provision/resize/
   refresh threading from #980). Keep the #980 skill bake.
3. **Per-specialist identity in manual /chat too** — the agent reads its own identity file
   from its cwd, so it's correct on wake, manual chat, and cron alike (Paperclip-native).

## Implementation

1. **Per-agent keys** — `ecs_manager.write_user_configs`: replace the Friday-only mint
   (~line 2240) with a loop over **all** `fetch_paperclip_agents_for_owner` results, calling
   `ensure_paperclip_api_key(owner_id, agent_id, admin_client)` for each → `{agent_id: token}`
   map. Resolve `company_id` once. Signature of `ensure_paperclip_api_key` unchanged
   (already per-agent; Secrets path `isol8/<env>/paperclip-agent-keys/<owner>/<agent_id>`).

2. **Per-agent identity files** — new helper (e.g. `pre_stage_paperclip_identities` next to
   `pre_stage_friday_identity` in `friday_identity.py`) writes, for each agent,
   `workspaces/{agent_id}/paperclip-claimed-api-key.json` =
   `{"token","agentId","companyId","apiUrl"}` via `Workspace.write_file_if_missing`
   (O_EXCL + chown 1000). Call it from `write_user_configs` in the same block as
   `pre_stage_friday_identity` so reprovision/resize/refresh re-stage idempotently.

3. **Per-agent adapter path** — `paperclip_adapter_config.synthesize_openclaw_adapter`: set
   per-agent `adapterConfig.claimedApiKeyPath =`
   `~/.openclaw/workspaces/{agent_id}/paperclip-claimed-api-key.json` (default singular path
   collides). Verify how/where the adapter config is applied per agent (Paperclip admin
   client agent create/update) and thread the per-agent path through.

4. **Seed rework** — the AGENTS/SOUL template (`friday_identity.py` `_AGENTS_TEMPLATE`,
   ~267-459) is MCP-centric ("interact through the paperclip MCP server"). Rewrite to:
   (a) drop all MCP references; (b) instruct the agent to use the bundled `paperclip` skill
   (curl) for Paperclip; (c) instruct: "Your Paperclip identity (token/agentId/companyId/
   apiUrl) is in `./paperclip-claimed-api-key.json` in your workspace — load it before any
   Paperclip call (e.g. `export PAPERCLIP_API_KEY=$(jq -r .token ./paperclip-claimed-api-key.json)`,
   same for AGENT_ID/COMPANY_ID/API_URL), and set `X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID`
   from the wake text on mutating calls." The **vendored skill stays unmodified** (it's
   env-based; the agent populates its own run-context env from its own file).

5. **Remove the MCP** — `config.py`: delete the `mcp.servers.paperclip` block (~1240-1262)
   and its `PAPERCLIP_*` env. Leave other MCP servers (integration servers) intact.

6. **Remove the static env** — `ecs_manager.py`: delete `_paperclip_runtime_env` and its
   three call sites (`provision_user_container`, `resize_user_container`,
   `refresh_task_definition`). Keep the rest of those methods unchanged.

## Files
- `apps/backend/core/containers/ecs_manager.py` (key loop; remove env helper + 3 calls)
- `apps/backend/core/services/friday_identity.py` (per-agent identity staging + seed rework)
- `apps/backend/core/services/paperclip_adapter_config.py` (per-agent claimedApiKeyPath)
- `apps/backend/core/containers/config.py` (remove MCP block)
- `apps/backend/core/services/paperclip_api_key.py` (reused as-is)
- Tests: per-agent key loop, identity-file staging, adapter path, MCP removed, env removed,
  seed no longer references MCP.

## Claimed-key file format
`{"token": "<key>", "agentId": "<id>", "companyId": "<company>", "apiUrl": "<url>"}`
(seed reads with `jq -r .token` etc.). Lock this; it's Isol8-defined.

## Risks
- Soft contract: identity correctness depends on the agent loading its file + copying RUN_ID
  from the wake text (native Paperclip design; prompt-dependent, not enforced).
- Key rotation: `write_if_missing` won't overwrite; fine for stable keys (Secrets value
  idempotent). Force-overwrite is a later concern.
- Adapter protocol v3 must keep being accepted by the container image.
- Per-agent ≠ per-member: keys are per Paperclip agent_id (shared across org members).

## Validation (deferred per owner — skill is fine)
Live: a non-Friday agent run → Paperclip audit shows that agent (not Friday); a routine it
creates is self-assigned. Needs a runnable agent (dev container, or prod post-Jun-26/Bedrock).
