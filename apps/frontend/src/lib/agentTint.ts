/**
 * Identity colour for an agent's avatar tile.
 *
 * Every agent used to render the same neutral tile with the same blue presence
 * dot, so a roster of six agents was six identical squares — the opposite of
 * what a platform built on distinct agents should say. Three tints, assigned by
 * a stable hash of the agent's id, fix that without inventing colour: they are
 * drawn from the §06 state palette and resolve through --agent-tint-* so focus
 * mode desaturates them along with the rest of the hue layer.
 *
 * Deliberately NOT applied to the presence dot. Presence ("this agent is
 * reachable") and identity ("this is Atlas") mean different things, and giving
 * them the same channel would make both unreadable.
 */

const TINTS = ["var(--agent-tint-1)", "var(--agent-tint-2)", "var(--agent-tint-3)"] as const;

/**
 * FNV-1a. Chosen over a sum-of-char-codes because near-identical ids — the
 * `agent_<uuid>` values this is fed, which can share long prefixes — must not
 * collide onto the same tint just because they differ late in the string.
 */
function hash(input: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < input.length; i += 1) {
    h ^= input.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

/** Stable across reloads, sessions and devices: it depends only on the id. */
export function agentTint(agentId: string): string {
  return TINTS[hash(agentId) % TINTS.length];
}

export const AGENT_TINT_COUNT = TINTS.length;
