// Pure resolver for the AWS connector exec profile.
//
// Kept free of the OpenClaw plugin SDK so it can be unit-tested with plain
// `node --test` outside the container (index.js imports the SDK, which only
// resolves inside the OpenClaw image via the host's self-referencing
// `openclaw` package).
import { readFileSync } from "node:fs";

// Must match the materializer's AWS_CONNECTOR_PROFILE constant — the Plan-2/3
// credential rail writes the owner's role under this exact profile name.
export const AWS_CONNECTOR_PROFILE = "lightbulb-connector";

// The materializer writes the profile to the file at AWS_CONFIG_FILE; this is
// the baked container default (same value the task env sets).
export const DEFAULT_AWS_CONFIG_PATH = "/home/node/.openclaw/.aws/config";

// The literal section header an `aws` config file uses for a named profile.
export const AWS_CONNECTOR_PROFILE_HEADER = `[profile ${AWS_CONNECTOR_PROFILE}]`;

// Line-anchored matcher for a REAL INI section header. A plain substring scan
// would false-positive on a commented-out header (e.g. `# [profile
// lightbulb-connector]`) and inject AWS_PROFILE for a profile the awscli can't find
// ("profile not found"). Derived from AWS_CONNECTOR_PROFILE (regex-escaped) so
// it stays consistent with the constant — matches `[profile lightbulb-connector]`
// only when it starts a line.
const HEADER_RE = new RegExp(
  `(?:^|\\n)\\[profile ${AWS_CONNECTOR_PROFILE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\](?:\\n|$)`,
);

/**
 * Decide whether the agent's exec'd commands should run as the owner's
 * connector AWS identity.
 *
 * Returns `{ AWS_PROFILE: "lightbulb-connector" }` only when the config file at
 * `configPathEnv` (falling back to the baked default) exists AND declares the
 * `[profile lightbulb-connector]` section. Otherwise returns `{}`. Never throws —
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
    if (HEADER_RE.test(contents)) {
      return { AWS_PROFILE: AWS_CONNECTOR_PROFILE };
    }
    return {};
  } catch {
    return {};
  }
}
