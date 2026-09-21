import type { Connection } from "@/hooks/useConnections";
import type { RoomParticipant } from "./types";

// =============================================================================
// The onboarding setup band's state — derived, never stored.
//
// Every step's done-state comes from data the room already holds, so there is
// no completion record to write, migrate, or let drift out of sync with
// reality. Disconnect Stripe and its card comes back on its own.
// =============================================================================

export type SetupStepId = "stripe" | "github" | "notion" | "invite" | "name-agents";

export interface SetupStep {
  id: SetupStepId;
  label: string;
  detail: string;
  done: boolean;
  /**
   * The backend refuses this action for a plain member, so the card renders
   * disabled rather than handing them a 403 on click. Connections are gated by
   * `require_org_admin` (routers/connections.py) and so is the workspace join
   * link (routers/orgs.py `_require_admin_of`). `PATCH /teams/agents/{id}` is
   * NOT gated, which is why naming is the one step a member can finish.
   */
  adminOnly: boolean;
}

/**
 * The connector cards, in display order. Every one is in `providers.yaml`.
 *
 * Three, not four: the fourth slot in the grid is "See more connections",
 * which opens the Connections panel. That is a LINK, not a step — it is never
 * done, carries no state, and is deliberately absent from this list so
 * `isBandComplete` cannot wait on it forever.
 */
export const CONNECTOR_STEPS: readonly { id: SetupStepId; label: string; detail: string }[] = [
  { id: "stripe", label: "Stripe", detail: "Revenue, customers and payouts" },
  { id: "github", label: "GitHub", detail: "Code, issues and pull requests" },
  { id: "notion", label: "Notion", detail: "Docs and company knowledge" },
];

/**
 * The names `paperclip_provisioning.STARTER_ROSTER` seeds a fresh workspace
 * with. An agent still carrying one has never been named by anyone.
 *
 * Duplicated from the backend deliberately — the alternative is a round trip
 * for six string literals that change roughly never. If the roster there ever
 * changes, the cost here is a card that stays visible slightly too long, which
 * is why this is safe to duplicate and a credential would not be.
 */
export const STARTER_AGENT_DEFAULT_NAMES: ReadonlySet<string> = new Set([
  "CEO",
  "Engineering",
  "Finance",
  "Growth",
  "Analytics",
  "Customer Operations",
]);

export interface SetupStepInputs {
  participants: readonly RoomParticipant[];
  connections: readonly Connection[];
  /**
   * Live agent id -> name, from the agent roster.
   *
   * A room member's `displayName` is a SNAPSHOT: `upsert_member` writes it when
   * membership changes, and `PATCH /teams/agents/{id}` never rewrites it. So a
   * rename in Teams leaves the member row saying "CEO" forever, and deriving
   * from it alone would mean the card could never be completed. The roster is
   * the live truth; the member row is the fallback while it loads.
   */
  agentNames: ReadonlyMap<string, string>;
}

export function buildSetupSteps({ participants, connections, agentNames }: SetupStepInputs): SetupStep[] {
  const connected = new Set(
    connections.filter((entry) => entry.status === "connected").map((entry) => entry.provider),
  );
  const humans = participants.filter((participant) => participant.kind === "human");
  const agents = participants.filter((participant) => participant.kind === "agent");

  return [
    ...CONNECTOR_STEPS.map((connector) => ({
      ...connector,
      done: connected.has(connector.id),
      adminOnly: true,
    })),
    {
      id: "invite" as const,
      label: "Invite your teammates",
      detail: "Share the workspace join link",
      done: humans.length > 1,
      adminOnly: true,
    },
    {
      id: "name-agents" as const,
      label: "Name your agents",
      // Scoped to the room's agents rather than the whole roster: this channel
      // is seeded with exactly the starter six, so a later marketplace hire —
      // which arrives with a name of its own — cannot mark the step done on the
      // owner's behalf. Names still resolve through the live roster.
      detail: "Make the starter team yours",
      done: agents.some(
        (agent) => !STARTER_AGENT_DEFAULT_NAMES.has(agentNames.get(agent.id) ?? agent.displayName),
      ),
      adminOnly: false,
    },
  ];
}

export function isBandComplete(steps: readonly SetupStep[]): boolean {
  return steps.every((step) => step.done);
}
