// Pure resolver for the per-agent Paperclip exec credentials.
//
// Kept free of the OpenClaw plugin SDK so it can be unit-tested with plain
// `node --test` outside the container (index.js imports the SDK, which only
// resolves inside the OpenClaw image via the host's self-referencing
// `openclaw` package). Mirrors the aws-exec-profile layout next door.
//
// WHY THIS EXISTS: Paperclip's cloud adapter does not inject a run JWT the way
// the local adapter does, and the key cannot live in container-wide task env
// because ONE container hosts MANY agents, each needing its own key. So the
// backend writes a per-agent file (friday_identity.py) and the seeded AGENTS.md
// used to tell the agent to `export PAPERCLIP_API_KEY=$(jq -r .token …)` before
// every Paperclip call.
//
// That instruction leaked. `export` does not survive between exec calls (each
// is a fresh child), so the model had to re-materialize the values every time,
// and it printed them into its visible reasoning — a long-lived Paperclip key
// in the owner's chat transcript, which is persisted to SQLite and replicated
// to S3. Observed live on dev 2026-08-14.
//
// Injecting here means the token exists only in the exec child's environment.
// The model never sees it, so it cannot echo what it does not have.
import { readFileSync } from "node:fs";

// Agent workspaces are `<root>/workspace-<agentId>` — see
// core/containers/config.py (agents.list[].workspace) and friday_identity.py,
// which writes the key file at `workspace-<agentId>/<KEY_FILENAME>`.
export const DEFAULT_WORKSPACE_ROOT = "/home/node/.openclaw";
export const KEY_FILENAME = "paperclip-claimed-api-key.json";

// The file shape is Isol8-defined and locked (friday_identity.py says so).
// Map each field to the env var the skill and the Paperclip wake text expect.
const FIELD_TO_ENV = {
  token: "PAPERCLIP_API_KEY",
  agentId: "PAPERCLIP_AGENT_ID",
  companyId: "PAPERCLIP_COMPANY_ID",
  apiUrl: "PAPERCLIP_API_URL",
};

/**
 * Build the per-agent credentials for an exec child.
 *
 * Returns `{}` — never throws — when anything is off: no agentId on the hook
 * context, no key file (an agent that was never claimed, or a non-Paperclip
 * agent), unreadable file, malformed JSON, or a field that is not a non-empty
 * string. Degrading to `{}` is deliberate: a failed read must never break the
 * exec tool, and a partially-populated env is worse than none, because the
 * agent would authenticate as nobody with a confusing error.
 *
 * Read fresh on every call rather than cached: keys are re-seeded on
 * reprovision and re-claim, and an exec is cheap relative to a file read.
 *
 * @param {string | undefined} agentId hook context agentId
 * @param {string | undefined} workspaceRootEnv optional root override
 * @returns {Record<string, string>}
 */
export function resolve(agentId, workspaceRootEnv) {
  if (typeof agentId !== "string" || agentId.length === 0) {
    return {};
  }
  // Defensive: agentId reaches us from the runtime, and it is interpolated
  // into a filesystem path. Anything with a separator or traversal is not a
  // real agent id (they are `agent_<uuid>`-shaped) and must not escape the
  // workspace root.
  if (agentId.includes("/") || agentId.includes("\\") || agentId.includes("..")) {
    return {};
  }
  const root =
    typeof workspaceRootEnv === "string" && workspaceRootEnv.length > 0
      ? workspaceRootEnv
      : DEFAULT_WORKSPACE_ROOT;
  try {
    const raw = readFileSync(`${root}/workspace-${agentId}/${KEY_FILENAME}`, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return {};
    }
    const env = {};
    for (const [field, envVar] of Object.entries(FIELD_TO_ENV)) {
      const value = parsed[field];
      if (typeof value !== "string" || value.length === 0) {
        // All-or-nothing: a half-set env authenticates as nobody.
        return {};
      }
      env[envVar] = value;
    }
    return env;
  } catch {
    return {};
  }
}
