// Single source of panel identity for the dockview workspace. The URL
// reconciler, the controller, the persistence sanitizer, and the shell's
// components map all derive from this file — nothing else may invent a
// panel id or component name.

export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "browser" | "missing";

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
  "teams-costs": "teams-dashboard",
};

// One-shot intents: copied to the target panel's open params, immediately
// stripped from the URL, cleared after consumption, excluded from the
// persisted layout (dockview serializes panel params — an unstripped intent
// would re-fire its dialog on every restore).
export const ONE_SHOT_PARAMS = ["new", "newIssue", "action"] as const;

// Billing-return params: not commands. useCheckoutReturn owns their strips.
export const NON_COMMAND_PARAMS = ["checkout", "subscription", "credits", "provider"] as const;

const TEAMS_LIST_KEYS: Readonly<Record<string, string>> = {
  dashboard: "Home",
  agents: "Agents",
  "org-chart": "Org chart",
  inbox: "Inbox",
  issues: "Tasks",
  routines: "Automations",
  goals: "Goals",
  projects: "Projects",
  activity: "Activity",
  artifacts: "Library",
  search: "Search",
  settings: "Team Settings",
};

const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Automation",
  "project-detail": "Project",
  "goal-detail": "Goal",
  "run-detail": "Run",
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
  llm: "Model & Credits",
  integrations: "Integrations",
  dreams: "Dreams",
  "skill-proposals": "Skill Proposals",
};

export const PANEL_COMPONENT_KEYS: ReadonlySet<string> = new Set([
  "chat",
  "teams",
  "control",
  "file-viewer",
  "browser",
  "missing",
]);

// Key-map membership must be OWN-property only: the `in` operator walks the
// prototype chain, so Object.prototype keys ("constructor", "valueOf", …) in
// a crafted ?panel URL would pass validation and render inherited functions
// as titles/components instead of rejecting to null.
function hasOwnKey(map: Readonly<Record<string, string>>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(map, key);
}

export function resolvePanelAlias(panelKey: string): string {
  return hasOwnKey(RETIRED_PANEL_ALIASES, panelKey) ? RETIRED_PANEL_ALIASES[panelKey] : panelKey;
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
    // Durable: the viewer must keep browsing the workspace of the chat that
    // opened it (ChatPanel passes its resolved agent), not whichever chat is
    // focused later — and a layout restore re-mounts with the same binding.
    if (params.agent) p.agent = params.agent;
    return { panelId: "file-viewer", component: "file-viewer", title: "Files", params: p };
  }

  if (key === "browser") {
    // Singleton, no params: the container has ONE browser (one Xvfb display),
    // so a second open focuses the existing panel rather than double-viewing.
    return { panelId: "browser", component: "browser", title: "Browser", params: {} };
  }

  if (key === "agent-inspector") {
    const p: Record<string, string> = { controlKey: "agent-inspector" };
    if (params.agent) p.agentId = params.agent;
    if (params.inspectorTab) p.initialTab = params.inspectorTab;
    return { panelId: "agent-inspector", component: "control", title: "Agent Inspector", params: p };
  }

  if (key.startsWith("teams-")) {
    const teamsKey = key.slice("teams-".length);
    if (hasOwnKey(TEAMS_DETAIL_KEYS, teamsKey)) {
      const detailId = params.detailId;
      if (!detailId) return null;
      return {
        panelId: `${key}:${detailId}`,
        component: "teams",
        title: TEAMS_DETAIL_KEYS[teamsKey],
        params: { teamsKey, detailId },
      };
    }
    if (hasOwnKey(TEAMS_LIST_KEYS, teamsKey)) {
      const p: Record<string, string> = { teamsKey };
      // Durable filters ride along; one-shots are handled by the reconciler
      // at open time and never enter these params.
      if (teamsKey === "issues" && params.agent) p.agent = params.agent;
      // ?tab deep links (bookmarks predating the URL-sync removal still carry
      // them) select the inbox band; tab clicks never write it back.
      if (teamsKey === "inbox" && params.tab) p.tab = params.tab;
      return { panelId: key, component: "teams", title: TEAMS_LIST_KEYS[teamsKey], params: p };
    }
    return null;
  }

  if (hasOwnKey(CONTROL_KEYS, key)) {
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
  // param or a refresh resurrects the panel the user just closed. "panel" +
  // "agentId" cover the explicit ?panel=chat grammar (describePanel accepts
  // agent OR agentId) so an externally-authored chat link clears fully.
  if (panelId.startsWith("chat:")) return ["panel", "agent", "agentId"];
  if (panelId === "file-viewer") return ["panel", "path", "agent"];
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === "teams-issues") return ["panel", "agent"];
  if (panelId === "teams-inbox") return ["panel", "tab"];
  if (/^teams-(issue|routine|project|goal|run)-detail:/.test(panelId)) return ["panel", "detailId"];
  return ["panel"];
}

// PANEL-param keys (descriptor.params space — agentId/initialTab, not the URL
// grammar's agent/inspectorTab) that a plain re-open of an already-open panel
// must CLEAR when the fresh descriptor doesn't carry them. dockview's
// updateParameters merges, so without an explicit `undefined` a stale durable
// param (an agent filter, an inspector tab) survives an unfiltered navigation
// — breaking the mobile invariant that plain panel nav drops stale state.
export function clearablePanelParamKeys(panelId: string): string[] {
  if (panelId === "teams-issues") return ["agent"];
  if (panelId === "teams-inbox") return ["tab"];
  if (panelId === "file-viewer") return ["path", "agent"];
  if (panelId === "agent-inspector" || panelId === "control:agents") {
    return ["agentId", "initialTab"];
  }
  return [];
}
