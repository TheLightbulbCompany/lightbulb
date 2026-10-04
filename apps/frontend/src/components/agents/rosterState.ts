// An agent's runtime state as the roster hook and the messaging directory show
// it. Which statuses mean what is NOT decided here: see
// `teams/shared/lib/agentStatus`.

import type { Agent } from "@/hooks/useAgents";
import { classifyAgentStatus } from "@/components/teams/shared/lib/agentStatus";

/**
 * The roster's view of an agent's runtime state.
 *
 * Derived from the shared `CompanyAgent.status` vocabulary
 * (`teams/shared/lib/agentStatus`) — NOT from a second table of status
 * strings maintained here. One backend enum, one classification.
 *
 * `idle` is the absence state: it renders no marker at all. Only agents with
 * something to report get a dot, a word and a rail — so a calm roster looks
 * calm, and the eye goes straight to the rows that don't.
 *
 * `terminated` is deliberately NOT folded into `paused`. A paused agent is one
 * click from resuming; a terminated one is permanently dead and will quietly
 * vanish from assignee pickers. Rendering both as "Paused" also put this
 * surface in direct contradiction with the header's pause/resume control,
 * which reads `status === "paused"` off the identical field and so offers
 * "Pause agent" for an agent the roster claimed was already paused.
 *
 * `unknown` is a status the shared vocabulary doesn't recognise. See
 * `rosterStateFor` for why that is a state rather than a default.
 */
export type RosterState =
  | "running"
  | "attention"
  | "paused"
  | "terminated"
  | "idle"
  | "unknown";

/**
 * Map a `CompanyAgent.status` onto a roster state via the shared vocabulary.
 *
 * **On unrecognised statuses.** This used to `default: return "idle"`, which
 * was wrong in the most damaging possible direction: `active`, `online`,
 * `thinking` and `failed` are all real, currently-emitted values, so a live
 * agent rendered with no rail and a *failed* one rendered as if nothing had
 * happened — the same class of lie as the hardcoded green dot this roster
 * replaced, just inverted.
 *
 * The fix is the shared table, but the default still has to be decided, and
 * both obvious answers are lies: `idle` claims nothing is happening, `running`
 * claims something is. So an unclassifiable status gets its own state and the
 * roster shows the raw value instead of guessing. That keeps us honest, and it
 * makes the next enum drift *visible* in the sidebar on day one rather than
 * silently mis-rendering until someone notices.
 */
export function rosterStateFor(status: string | null | undefined): RosterState {
  switch (classifyAgentStatus(status)) {
    case "live":
      return "running";
    case "attention":
      return "attention";
    case "paused":
      return "paused";
    case "terminated":
      return "terminated";
    case "idle":
      return "idle";
    default:
      // Absent status = the agent simply isn't reporting (a roster read that
      // hasn't landed yet); that IS idle. A present-but-unrecognised one is
      // information we can't classify, and gets shown verbatim.
      return status ? "unknown" : "idle";
  }
}

export interface RosterEntry {
  agent: Agent;
  state: RosterState;
  /** The per-owner CEO / orchestrator. Pinned first, always. */
  isCeo: boolean;
  /** The raw `CompanyAgent.status`, carried only so a `state: "unknown"` row
   *  can show the value verbatim instead of inventing a label for it. */
  rawStatus?: string | null;
  /** Backend-authored starter provenance, retained for roster consumers. */
  isSeeded?: boolean;
}
