/**
 * Frontend mirror of OpenClaw's `normalizeAgentId`
 * (see openclaw `src/routing/session-key.ts`). Used by the two agent-create
 * surfaces (`components/chat/AgentDialogs.tsx#AgentCreateDialog` and
 * `components/control/panels/AgentCreateForm.tsx`) to derive the agent id
 * the backend will assign at create time, so the UI can preview the id
 * and reject duplicates client-side.
 *
 * Two surfaces, not one: the dialog renders as a modal popover from the
 * /chat sidebar, the form renders as an inline panel inside the control
 * Agents panel. Both are live UX surfaces — they share the normalize +
 * validate logic but not the surrounding UI shell.
 */
export function normalizeAgentId(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-")
    .replace(/^-|-$/g, "");
}

/** Agent ids the system reserves and rejects on create. */
export const RESERVED_AGENT_IDS: readonly string[] = ["main"];

/**
 * Validate a proposed agent name. Returns either `null` (valid) or a
 * user-facing error string. Caller is responsible for `name.trim() !== ""`
 * gating — an empty name is "not yet typed" rather than an error.
 */
export function validateAgentName(name: string, existingIds: string[]): string | null {
  const id = normalizeAgentId(name);
  if (id === "") return null;
  if (existingIds.includes(id)) return "An agent with this name already exists";
  if (RESERVED_AGENT_IDS.includes(id)) return "This name is reserved";
  return null;
}
