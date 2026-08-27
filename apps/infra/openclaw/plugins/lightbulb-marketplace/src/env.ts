/**
 * Environment resolution and URL building — the pure half of the plugin.
 *
 * Kept separate from index.ts so it is testable without loading the OpenClaw
 * plugin SDK (and therefore the whole agent runtime) into the test process.
 */

export interface LightbulbEnv {
  base: string;
  ownerId: string;
  token: string;
}

export type RawEnv = Record<string, string | undefined>;

/**
 * Read backend base URL, owner id, and the container's hook credential.
 *
 * A BAKED plugin is byte-identical in every container, so unlike Atlas's
 * per-owner seed files (which substitute `{{LIGHTBULB_BRIDGE_URL}}` at stage time)
 * it cannot carry owner identity in its source. It must read it from the
 * environment — hence LIGHTBULB_OWNER_ID, injected per task definition.
 *
 * LIGHTBULB_OWNER_ID is an identifier, not a credential. OPENCLAW_HOOK_TOKEN is
 * what actually authenticates the call, and the backend compares it
 * timing-safely against the named owner's own container row — so naming
 * another owner does not grant access to them.
 */
export function resolveEnv(env: RawEnv): LightbulbEnv {
  const base = (env.LIGHTBULB_API_URL ?? "").trim().replace(/\/+$/, "");
  const ownerId = (env.LIGHTBULB_OWNER_ID ?? "").trim();
  const token = (env.OPENCLAW_HOOK_TOKEN ?? "").trim();
  if (!base) throw new Error("LIGHTBULB_API_URL is not set on this container");
  if (!ownerId) throw new Error("LIGHTBULB_OWNER_ID is not set on this container");
  if (!token) throw new Error("OPENCLAW_HOOK_TOKEN is not set on this container");
  return { base, ownerId, token };
}

/** Public, unauthenticated search — compact projection only. */
export function buildSearchPath(query: string): string {
  const qs = new URLSearchParams({ view: "compact", q: query });
  return `/api/v1/marketplace/listings/search?${qs}`;
}

/**
 * Hook-token-authenticated recommend resolver.
 *
 * Owner goes in the PATH, matching the convention every other hook-token
 * route in the backend uses (hooks_proxy, agent_builder): `/u/{owner_id}/...`.
 */
export function buildRecommendPath(
  ownerId: string,
  slugs: string[],
  issueId: string | undefined,
): string {
  const qs = new URLSearchParams({ slugs: slugs.join(",") });
  if (issueId) qs.set("issue_id", issueId);
  return `/api/v1/marketplace/u/${encodeURIComponent(ownerId)}/recommend?${qs}`;
}
