// paperclip-exec-creds — baked OpenClaw plugin (no fork change).
//
// SYNC NOTE: this file and `./dist/index.js` are parallel plugin-registration
// wrappers and MUST be kept in sync (identical hook wiring). They differ only
// in the resolver import path. The resolver logic itself is single-sourced in
// `./dist/resolve.js` — edit the resolver THERE only, never duplicate it here.
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
//
// This file is the source twin the 6.6 plugin loader keys discovery on:
// `package.json#openclaw.extensions` drives discovery (the loader iterates
// `extensions` and only resolves a plugin when that array is non-empty), while
// `runtimeExtensions[i]` names the built JS the loader actually loads — here
// `./dist/index.js`. The `dist/` here is HAND-WRITTEN, not build output, so
// the resolver below is imported straight from `./dist/resolve.js` (the single
// resolver implementation, also covered by index.test.mjs) rather than a
// separate compiled copy.
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { resolve } from "./dist/resolve.js";

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
