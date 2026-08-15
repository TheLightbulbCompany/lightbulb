// paperclip-exec-creds — baked OpenClaw plugin (no fork change).
//
// SYNC NOTE: this file and `../index.ts` are parallel plugin-registration
// wrappers and MUST be kept in sync (identical hook wiring). They differ only
// in the resolver import path. The resolver logic itself is single-sourced in
// `./resolve.js` — edit the resolver THERE only, never duplicate it here.
//
// Registers a `resolve_exec_env` hook that contributes this agent's Paperclip
// credentials to its exec'd commands ONLY. The exec tool builds the child env
// as a copy of process.env plus this override layer (bash-tools.exec.ts) and
// never mutates process.env, so nothing else in the container sees the key.
//
// Per-AGENT, not per-container: one container hosts many agents and each has
// its own Paperclip key, so the hook context's agentId is what selects the
// file. `PAPERCLIP_*` passes the host exec env key policy (it is in no blocked
// key list or prefix), so all four vars reach the child.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolve } from "./resolve.js";

// resolve_exec_env handler: contribute this agent's Paperclip identity.
// Second arg is the hook context (PluginHookAgentContext) — agentId is the
// only field we need, and the only one the exec tool populates for us.
function resolveExecEnv(_event, ctx) {
  return resolve(ctx?.agentId, process.env.OPENCLAW_WORKSPACE_ROOT);
}

export default definePluginEntry({
  id: "paperclip-exec-creds",
  name: "Paperclip exec credentials",
  description:
    "Injects this agent's PAPERCLIP_* credentials into its exec environment only.",
  register(api) {
    api.on("resolve_exec_env", resolveExecEnv, { priority: 50 });
  },
});
