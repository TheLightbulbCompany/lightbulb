// Pure resolver for the AWS read-only exec profile.
//
// Kept free of the OpenClaw plugin SDK so it can be unit-tested with plain
// `node --test` outside the container (index.js imports the SDK, which only
// resolves inside the OpenClaw image via the host's self-referencing
// `openclaw` package).
import { readFileSync } from "node:fs";

// Must match the materializer's AWS_READONLY_PROFILE constant — the Plan-2/3
// credential rail writes the owner's role under this exact profile name.
export const AWS_READONLY_PROFILE = "isol8-readonly";

// The materializer writes the profile to the file at AWS_CONFIG_FILE; this is
// the baked container default (same value the task env sets).
export const DEFAULT_AWS_CONFIG_PATH = "/home/node/.openclaw/.aws/config";

// The literal section header an `aws` config file uses for a named profile.
export const AWS_READONLY_PROFILE_HEADER = `[profile ${AWS_READONLY_PROFILE}]`;

/**
 * Decide whether the agent's exec'd commands should run as the owner's
 * read-only AWS identity.
 *
 * Returns `{ AWS_PROFILE: "isol8-readonly" }` only when the config file at
 * `configPathEnv` (falling back to the baked default) exists AND declares the
 * `[profile isol8-readonly]` section. Otherwise returns `{}`. Never throws —
 * any fs error (missing file, permission, etc.) degrades to `{}` so a failed
 * read can never break the exec tool.
 *
 * Read fresh on every call: an `aws` exec is infrequent, so correctness
 * (connect/disconnect reflected immediately) beats caching.
 *
 * @param {string | undefined} configPathEnv value of process.env.AWS_CONFIG_FILE
 * @returns {{ AWS_PROFILE: string } | {}}
 */
export function resolve(configPathEnv) {
  try {
    const path =
      typeof configPathEnv === "string" && configPathEnv.length > 0
        ? configPathEnv
        : DEFAULT_AWS_CONFIG_PATH;
    const contents = readFileSync(path, "utf8");
    if (contents.includes(AWS_READONLY_PROFILE_HEADER)) {
      return { AWS_PROFILE: AWS_READONLY_PROFILE };
    }
    return {};
  } catch {
    return {};
  }
}
