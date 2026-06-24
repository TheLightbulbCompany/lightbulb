/**
 * Slugify an agent name into OpenClaw's `normalizeAgentId` shape
 * (see openclaw `src/routing/session-key.ts`).
 *
 * NOTE: this slug is NOT the id the backend assigns. Agent creation is
 * intercepted server-side and Paperclip mints a UUID for the real
 * workspace/agent id (e.g. `6f23fa6c-…`). So this slug must never be shown
 * as "the id" — it is used only as a stable key for client-side
 * name-uniqueness checks (comparing one proposed name against the existing
 * names, both normalized the same way).
 */
export function normalizeAgentId(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/** Names the system reserves and rejects on create. */
export const RESERVED_AGENT_IDS: readonly string[] = ["main"];

/**
 * Validate a proposed agent name against the existing agents' display
 * names. Returns either `null` (valid) or a user-facing error string.
 * Caller is responsible for `name.trim() !== ""` gating — an empty name is
 * "not yet typed" rather than an error.
 *
 * `existingNames` are the other agents' display names (not their UUID ids):
 * we normalize both sides so "Echo" collides with an existing "Echo".
 * Comparing against the real UUID ids — as this once did — never matched a
 * slug, so the duplicate guard was silently dead and let two same-named
 * agents be created.
 */
export function validateAgentName(name: string, existingNames: string[]): string | null {
  const id = normalizeAgentId(name);
  if (id === "") return null;
  const existingSlugs = existingNames.map(normalizeAgentId);
  if (existingSlugs.includes(id)) return "An agent with this name already exists";
  if (RESERVED_AGENT_IDS.includes(id)) return "This name is reserved";
  return null;
}
