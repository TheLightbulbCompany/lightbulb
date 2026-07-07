// aws-exec-profile — baked OpenClaw plugin (no fork change).
//
// SYNC NOTE: this file and `./dist/index.js` are parallel plugin-registration
// wrappers and MUST be kept in sync (identical hook wiring). They differ only
// in the resolver import path. The resolver logic itself is single-sourced in
// `./dist/resolve.js` — edit the resolver THERE only, never duplicate it here.
//
// Registers a `resolve_exec_env` hook that contributes AWS_PROFILE to the
// agent's exec'd commands ONLY. The exec tool builds the child env as a copy
// of process.env plus this override layer (bash-tools.exec.ts) and never
// mutates process.env, so the node process — and Bedrock's AWS SDK reading
// process.env in-process — stays on the host (ECS task role) credential chain.
// We only ever return AWS_PROFILE; the credential-pointer vars (AWS_CONFIG_FILE,
// AWS_ACCESS_KEY_ID, ...) are dropped by the host exec env key policy, but
// AWS_CONFIG_FILE is already set as task env and inherited via the process.env
// copy, so the awscli in the child resolves the profile from the same file.
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

// resolve_exec_env handler: contribute the owner's connector profile when it
// is materialized. Reads process.env.AWS_CONFIG_FILE fresh each call.
function resolveExecEnv() {
  return resolve(process.env.AWS_CONFIG_FILE);
}

export default definePluginEntry({
  id: "aws-exec-profile",
  name: "AWS exec profile",
  description:
    "Injects AWS_PROFILE=isol8-connector into the agent's exec environment only.",
  register(api) {
    api.on("resolve_exec_env", resolveExecEnv, { priority: 50 });
  },
});
