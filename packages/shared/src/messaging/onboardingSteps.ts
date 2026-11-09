import type { RoomMember } from "./types";

// Structural inputs, so web and mobile hand in whatever shape their own hooks
// return (web `useConnections`/`useBilling`, mobile's REST reads).
export type SetupParticipant = Pick<RoomMember, "kind" | "id" | "displayName">;
export interface SetupConnection { provider: string; status: string }
export interface SetupBilling { is_subscribed?: boolean | null; plan?: string | null; provider_choice?: string | null }

// =============================================================================
// The onboarding setup band's state — derived, never stored.
//
// Every step's done-state comes from data the client already holds, so there is
// no completion record to write, migrate, or let drift out of sync with
// reality. Disconnect Stripe and its card comes back on its own.
// =============================================================================

export type SetupStepId =
  | "starter-model" | "stripe" | "github" | "notion" | "invite" | "tell-ceo" | "approval" | "name-agents" | "context";

export interface SetupStep {
  id: SetupStepId;
  label: string;
  detail: string;
  /**
   * What the card says once it is done. Per-step, not a constant: "Connected"
   * is true of Stripe and nonsense on "Invite your team", which is the
   * bug this field exists to prevent.
   */
  doneDetail: string;
  done: boolean;
  /**
   * Whether the card stays clickable after it is done.
   *
   * Connecting Stripe twice is meaningless, so a done connector is inert. But
   * naming ONE of six agents marks the step done, and inviting one teammate
   * does not mean you never want the join link again — disabling those two
   * would strand the owner mid-task.
   */
  repeatable: boolean;
  /**
   * The backend refuses this action for a plain member, so the card renders
   * disabled rather than handing them a 403 on click. Connections are gated by
   * `require_org_admin` (routers/connections.py) and so is the workspace join
   * link only for replacing it. `PATCH /teams/agents/{id}` is NOT gated, and
   * any member may invite (D5).
   */
  adminOnly: boolean;
  /**
   * "Later" steps (naming, context) show under the core ones but neither count
   * toward "N of 6 done" nor keep the band open (08 §3).
   */
  later?: boolean;
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

/** Every connector shares one done-state word; the two action cards do not. */
const CONNECTED = "Connected";

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
  participants: readonly SetupParticipant[];
  connections: readonly SetupConnection[];
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
  /**
   * Clerk's `organization.membersCount`. Not #general's human members:
   * nothing adds a new workspace member to that room (only the owner is
   * seeded), so a teammate who joined would never flip the card.
   */
  workspaceMembers: number;
  /** `GET /billing/account`; null/undefined while unknown, which shows no billing card. */
  billing?: SetupBilling | null;
  /**
   * `GET /company/context`: whether the CEO's Hermes `memories/USER.md` holds
   * pasted context. Omitted (or null after a failed read) drops the step, so a
   * client with no paste door — mobile today — never renders a card it cannot
   * act on.
   */
  contextImported?: boolean | null;
  /**
   * A person has written in the starter channel ("Tell your CEO about the
   * business"). Omitted drops the step, like `contextImported`.
   */
  humanSpoke?: boolean;
  /**
   * Any approval in the workspace has been answered (approved or rejected).
   * Omitted (or null while unknown) drops the step.
   */
  approvalAnswered?: boolean | null;
}

/**
 * A paid Workspace/Ultra row with no provider runs on the capped starter model
 * (trial_cap.is_managed_capped). Base is excluded — the managed model IS that
 * plan — and so is the trial (plan null) and a lapsed row, which still carries
 * its old plan, which is why `is_subscribed` is checked rather than `plan` alone.
 */
function onStarterModel(billing: SetupStepInputs["billing"]): boolean {
  return Boolean(
    billing?.is_subscribed &&
      (billing.plan === "standard" || billing.plan === "ultra_pro") &&
      !billing.provider_choice,
  );
}

/**
 * 08 §3 order: invite first, then the business, one tool, the first approval;
 * naming and context come "later". Steps whose input a client does not supply
 * are omitted, so a client without that read never shows a card it cannot judge.
 */
export function buildSetupSteps({
  participants, connections, agentNames, workspaceMembers, billing, contextImported, humanSpoke, approvalAnswered,
}: SetupStepInputs): SetupStep[] {
  const status = new Map(connections.map((entry) => [entry.provider, entry.status] as const));
  const agents = participants.filter((participant) => participant.kind === "agent");

  return [
    // Present only while outstanding: choosing a provider drops it rather than
    // marking it done, so it can hold the band open but never close it early.
    // Admin-only because POST /billing/provider is (require_org_admin).
    ...(onStarterModel(billing)
      ? [{
          id: "starter-model" as const,
          label: "You're on the starter model",
          detail: "Choose OpenRouter managed or sign in with ChatGPT",
          doneDetail: "Provider chosen",
          done: false,
          repeatable: false,
          adminOnly: true,
        }]
      : []),
    {
      id: "invite" as const,
      label: "Invite your team",
      detail: "They see what your AI employees do and can approve it with you.",
      doneDetail: "Teammates joined",
      repeatable: true,
      done: workspaceMembers > 1,
      adminOnly: false, // any member may invite (D5)
    },
    ...(typeof humanSpoke === "boolean"
      ? [{
          id: "tell-ceo" as const,
          label: "Tell Lightbulb about the business",
          detail: "A website or a sentence",
          doneDetail: "Lightbulb has it",
          done: humanSpoke,
          repeatable: false,
          adminOnly: false,
        }]
      : []),
    // A connector this owner cannot start — gated, runtime-unsupported, or
    // absent from the catalog — is dropped, not shown dead: its authorize leg
    // would refuse, and a card nobody can finish would keep the band up forever.
    ...CONNECTOR_STEPS.filter((connector) => {
      const current = status.get(connector.id);
      return current !== undefined && current !== "unavailable";
    }).map((connector) => ({
      ...connector,
      doneDetail: CONNECTED,
      repeatable: false,
      done: status.get(connector.id) === "connected",
      adminOnly: true,
    })),
    ...(typeof approvalAnswered === "boolean"
      ? [{
          id: "approval" as const,
          label: "Answer your first approval",
          detail: "When a task needs your decision",
          doneDetail: "Answered",
          done: approvalAnswered,
          repeatable: true,
          adminOnly: false,
        }]
      : []),
    {
      id: "name-agents" as const,
      label: "Name your agents",
      // Scoped to the room's agents rather than the whole roster: this channel
      // is seeded with exactly the starter six, so a later marketplace hire —
      // which arrives with a name of its own — cannot mark the step done on the
      // owner's behalf. Names still resolve through the live roster.
      detail: "Make the starter team yours",
      doneDetail: "Named",
      repeatable: true,
      done: agents.some(
        (agent) => !STARTER_AGENT_DEFAULT_NAMES.has(agentNames.get(agent.id) ?? agent.displayName),
      ),
      adminOnly: false,
      later: true,
    },
    // Admin-only because POST /company/context writes the CEO's memory
    // (require_org_admin). Repeatable: a second paste augments the first.
    ...(typeof contextImported === "boolean"
      ? [{
          id: "context" as const,
          label: "Share your context",
          detail: "Paste what you've told Claude or ChatGPT",
          doneDetail: "Lightbulb has it",
          done: contextImported,
          repeatable: true,
          adminOnly: true,
          later: true,
        }]
      : []),
  ];
}

const CONNECTOR_IDS: ReadonlySet<SetupStepId> = new Set(CONNECTOR_STEPS.map((connector) => connector.id));

/** Steps that are always done: the workspace and its six AI employees (endowed progress, 08 §3). */
export const ENDOWED_STEPS = 2;

export function isConnectorStep(step: SetupStep): boolean {
  return CONNECTOR_IDS.has(step.id);
}

/**
 * "N of M done". The connector cards are ONE step ("Connect one tool"), done
 * when any is connected; "later" steps do not count; the two endowed steps do.
 */
export function setupProgress(steps: readonly SetupStep[]): { done: number; total: number } {
  const connectors = steps.filter(isConnectorStep);
  const units = steps.filter((step) => !step.later && !isConnectorStep(step)).map((step) => step.done);
  if (connectors.length) units.push(connectors.some((step) => step.done));
  return { done: ENDOWED_STEPS + units.filter(Boolean).length, total: ENDOWED_STEPS + units.length };
}

export function isBandComplete(steps: readonly SetupStep[]): boolean {
  const { done, total } = setupProgress(steps);
  return done === total;
}
