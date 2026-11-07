// The single vocabulary for `CompanyAgent.status`.
//
// The BFF forwards Paperclip's agent status verbatim, so this string is a
// backend enum that has drifted before and will drift again. It is read by
// more than one surface — the roster derives a rail/label/sort rank from it, the Teams pickers filter on it — and
// every surface that keeps its OWN list of recognised values is one status
// away from silently disagreeing with the others.
//
// So the CLASSIFICATION lives here, once. Presentation does not. What this module
// guarantees is that no surface can decide `failed` means "nothing is
// happening" while another decides it means "this broke".
//
// `classifyAgentStatus` returns `null` for anything not in the table — on
// purpose. Every caller must make its own deliberate decision about an
// unrecognised status rather than inheriting a default that quietly buckets a
// live agent into "idle". That default is exactly the bug this table exists to
// prevent.

/**
 * What a status MEANS, stripped of how any one surface draws it.
 *
 * - `live` — the agent is doing something right now. Includes `thinking`
 *   (working), `covered` (an active dependency is carrying it) and `stalled`
 *   (a live run that has gone quiet). All three are "not idle"; whether a
 *   surface wants to distinguish them further is a presentation choice.
 * - `attention` — stopped in a way that wants a human.
 * - `paused` — deliberately stopped and RESUMABLE. Still a usable agent.
 * - `terminated` — permanently dead. `paperclip_openclaw_pairing`'s
 *   `TERMINATED_AGENT_STATUS` calls this "not a usable agent"; it's what a
 *   rejected marketplace-hire's pre-created agent becomes, it's excluded from
 *   the owner's agent count, and the Teams pickers filter it out while leaving
 *   paused agents selectable. NOT a flavour of paused — there is nothing to
 *   resume.
 * - `idle` — available, nothing to report.
 */
export type AgentStatusClass = "live" | "attention" | "paused" | "terminated" | "idle";

/** Every `CompanyAgent.status` value the fleet is known to emit. */
export const AGENT_STATUS_CLASS: Record<string, AgentStatusClass> = {
  active: "live",
  online: "live",
  running: "live",
  thinking: "live",
  covered: "live",
  stalled: "live",
  error: "attention",
  failed: "attention",
  paused: "paused",
  terminated: "terminated",
  idle: "idle",
};

/** Classify a status, or `null` when it isn't in the vocabulary above. */
export function classifyAgentStatus(status: string | null | undefined): AgentStatusClass | null {
  if (!status) return null;
  return AGENT_STATUS_CLASS[status.toLowerCase()] ?? null;
}
