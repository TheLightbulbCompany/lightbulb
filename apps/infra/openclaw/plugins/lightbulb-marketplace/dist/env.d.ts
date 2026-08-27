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
export declare function resolveEnv(env: RawEnv): LightbulbEnv;
/** Public, unauthenticated search — compact projection only. */
export declare function buildSearchPath(query: string): string;
/**
 * Hook-token-authenticated recommend resolver.
 *
 * Owner goes in the PATH, matching the convention every other hook-token
 * route in the backend uses (hooks_proxy, agent_builder): `/u/{owner_id}/...`.
 */
export declare function buildRecommendPath(ownerId: string, slugs: string[], issueId: string | undefined): string;
