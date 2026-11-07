/**
 * The super-agent infra gate (contracts-v2 §1, spec 09 §7.1 L0/L1). Every super-agent resource exists only
 * where this is true. Flipping prod is a founder-approved
 * rollout PR, never a lane PR; `test/super-agent-prod-inert.test.ts` fails if prod synth carries any of it.
 */
export function superAgentEnabled(env: string): boolean {
  return env === "dev";
}
