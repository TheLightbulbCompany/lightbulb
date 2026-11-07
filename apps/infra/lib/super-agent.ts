/**
 * The super-agent infra gate (contracts-v2 §1, spec 09 §7.1 L0/L1). Every super-agent resource exists only
 * where this is true. On in dev and prod (founder go, 8 Nov 2026).
 */
export function superAgentEnabled(env: string): boolean {
  return env === "dev" || env === "prod";
}
