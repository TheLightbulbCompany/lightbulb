// Single source of panel identity for the dockview workspace. The URL
// reconciler, the controller, the persistence sanitizer, and the shell's
// components map all derive from this file — nothing else may invent a
// panel id or component name.

export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "missing";

export interface PanelDescriptor {
  panelId: string;
  component: PanelKind;
  title: string;
  params: Record<string, string>;
  renderer?: "always";
}

export const DEFAULT_AGENT_SENTINEL = "__default__";

// Merged from ControlPanelRouter (credits→llm) and TeamsPanelRouter
// (RETIRED_PANEL_ALIASES) — applied replace-only in the URL resolver so a
// Back landing on a retired key canonicalizes in place instead of pushing
// (the old ControlPanelRouter remap pushed, trapping Back on ?panel=credits).
export const RETIRED_PANEL_ALIASES: Readonly<Record<string, string>> = {
  credits: "llm",
  "teams-approvals": "teams-inbox",
  "teams-members": "teams-settings",
  "teams-skills": "teams-dashboard",
};

// One-shot intents: copied to the target panel's open params, immediately
// stripped from the URL, cleared after consumption, excluded from the
// persisted layout (dockview serializes panel params — an unstripped intent
// would re-fire its dialog on every restore).
export const ONE_SHOT_PARAMS = ["new", "newIssue", "action"] as const;

// Billing-return params: not commands. useCheckoutReturn owns their strips.
export const NON_COMMAND_PARAMS = ["checkout", "subscription", "credits", "provider"] as const;

const TEAMS_LIST_KEYS: Readonly<Record<string, string>> = {
  dashboard: "Dashboard",
  agents: "Agents",
  "org-chart": "Org Chart",
  inbox: "Inbox",
  issues: "Tasks",
  routines: "Routines",
  goals: "Goals",
  projects: "Projects",
  activity: "Activity",
  costs: "Costs",
  settings: "Team Settings",
};

const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Routine",
  "project-detail": "Project",
  "goal-detail": "Goal",
};

// Legacy control keys kept for URL grammar parity (ChatErrorCard links to
// ?panel=llm; AgentManageSection to ?panel=agents&agent=…). They open as
// dock panels but are NOT in the sidebar — /settings remains their home.
const CONTROL_KEYS: Readonly<Record<string, string>> = {
  overview: "Overview",
  sessions: "Sessions",
  channels: "Channels",
  usage: "Usage",
  agents: "Agents",
  skills: "Skills",
  llm: "Models & Providers",
  integrations: "Integrations",
  dreams: "Dreams",
  "skill-proposals": "Skill Proposals",
};

export const PANEL_COMPONENT_KEYS: ReadonlySet<string> = new Set([
  "chat",
  "teams",
  "control",
  "file-viewer",
  "missing",
]);

export function resolvePanelAlias(panelKey: string): string {
  return RETIRED_PANEL_ALIASES[panelKey] ?? panelKey;
}

export function chatPanelId(agentId: string): string {
  return `chat:${agentId}`;
}

export function describePanel(
  panelKey: string,
  params: Record<string, string>,
): PanelDescriptor | null {
  const key = resolvePanelAlias(panelKey);

  if (key === "chat") {
    const agentId = params.agent ?? params.agentId;
    if (!agentId) return null;
    return {
      panelId: chatPanelId(agentId),
      component: "chat",
      title: "Chat",
      params: { agentId },
      renderer: "always",
    };
  }

  if (key === "file-viewer") {
    const p: Record<string, string> = {};
    if (params.path) p.path = params.path;
    return { panelId: "file-viewer", component: "file-viewer", title: "Files", params: p };
  }

  if (key === "agent-inspector") {
    const p: Record<string, string> = { controlKey: "agent-inspector" };
    if (params.agent) p.agentId = params.agent;
    if (params.inspectorTab) p.initialTab = params.inspectorTab;
    return { panelId: "agent-inspector", component: "control", title: "Agent Inspector", params: p };
  }

  if (key.startsWith("teams-")) {
    const teamsKey = key.slice("teams-".length);
    if (teamsKey in TEAMS_DETAIL_KEYS) {
      const detailId = params.detailId;
      if (!detailId) return null;
      return {
        panelId: `${key}:${detailId}`,
        component: "teams",
        title: TEAMS_DETAIL_KEYS[teamsKey],
        params: { teamsKey, detailId },
      };
    }
    if (teamsKey in TEAMS_LIST_KEYS) {
      const p: Record<string, string> = { teamsKey };
      // Durable filters ride along; one-shots are handled by the reconciler
      // at open time and never enter these params.
      if (teamsKey === "issues" && params.agent) p.agent = params.agent;
      return { panelId: key, component: "teams", title: TEAMS_LIST_KEYS[teamsKey], params: p };
    }
    return null;
  }

  if (key in CONTROL_KEYS) {
    const p: Record<string, string> = { controlKey: key };
    if (key === "agents" && params.agent) p.agentId = params.agent;
    return { panelId: `control:${key}`, component: "control", title: CONTROL_KEYS[key], params: p };
  }

  return null;
}

// Which URL params "belong" to a panel when it is the URL-named panel —
// the clear-on-close replaceState clears exactly this set so a refresh
// can't resurrect a deliberately closed panel with stale filter state.
export function durableParamsFor(panelId: string): string[] {
  // Bare ?agent IS a chat command (urlChannel rule 7 — marketplace
  // deploy-success links), so closing a chat panel must clear the agent
  // param or a refresh resurrects the panel the user just closed.
  if (panelId.startsWith("chat:")) return ["agent"];
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === "teams-issues") return ["panel", "agent"];
  if (/^teams-(issue|routine|project|goal)-detail:/.test(panelId)) return ["panel", "detailId"];
  return ["panel"];
}
