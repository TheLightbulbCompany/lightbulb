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
  // Money is ONE page, and Payments/Products/Customers/Spend are its sections.
  // They alias to its key so the dock (which dedupes by panel id) opens ONE
  // "Money" tab no matter which of the five keys a link carries — Home's tiles
  // link three of them. describePanel synthesizes `section` from the RAW key
  // below.
  "teams-payments": "teams-money",
  "teams-products": "teams-money",
  "teams-customers": "teams-money",
  "teams-spend": "teams-money",
  // Activity folded into Inbox's "Recently" band. Aliased, not routed: the
  // dock dedupes by panel id, so a router-level mapping would leave an
  // Activity link opening a SECOND tab titled "Inbox".
  "teams-activity": "teams-inbox",
  // Goals and Projects are groupings of the Tasks page, not tabs of their own
  // — the same rows, cut a different way. Aliased for the same reason as
  // Money's four keys: the dock dedupes by panel id, so a router-level
  // mapping would open two tabs both titled "Tasks". describePanel
  // synthesizes `groupBy` from the RAW key below.
  "teams-goals": "teams-issues",
  "teams-projects": "teams-issues",
  // The org chart is the SAME roster read, drawn as a tree — a view of the
  // Agents panel, not a page of its own. Aliased for the same reason as the
  // four Money keys: the dock dedupes by panel id, so a router-level mapping
  // would open two tabs both titled "Agents". describePanel synthesizes
  // `view` from the RAW key below.
  "teams-org-chart": "teams-agents",
};

// The sections of the Money page — the set an absorbed key or a `?section=`
// deep link may name. Exported so TeamsPanelRouter narrows against the same
// list instead of keeping a second copy.
export const MONEY_SECTIONS: ReadonlySet<string> = new Set([
  "payments",
  "customers",
  "products",
  "spend",
]);

/** The three bands of the Inbox page, in render order. A ?section deep link
 *  scrolls to `inbox-<section>`; anything else is ignored. */
export const INBOX_SECTIONS: ReadonlySet<string> = new Set([
  "needs-you",
  "happening-now",
  "recently",
]);

/** Declared HERE, not in InboxPage, so the router and InboxPanel narrow to the
 *  same list the registry validates against. */
export type InboxSectionId = "needs-you" | "happening-now" | "recently";

/** The Tasks page's grouping dimensions — the set an absorbed key or a
 *  `?groupBy=` deep link may name. `TasksList.GROUP_OPTIONS` is the live list
 *  the menu renders; `TasksList.groupBy.test.tsx` pins the two together, so a
 *  new grouping cannot ship deep-linkable-but-unrendered (or the reverse). */
export const TASKS_GROUP_BY: ReadonlySet<string> = new Set([
  "none",
  "status",
  "assignee",
  "priority",
  "project",
  "goal",
  "parent",
]);

/** The Agents panel's two views — the set an absorbed key or a `?view=` deep
 *  link may name. The roster is the default; `chart` is the org chart. */
export const AGENTS_VIEWS: ReadonlySet<string> = new Set(["list", "chart"]);

// One-shot intents: copied to the target panel's open params, immediately
// stripped from the URL, cleared after consumption, excluded from the
// persisted layout (dockview serializes panel params — an unstripped intent
// would re-fire its dialog on every restore).
export const ONE_SHOT_PARAMS = ["new", "newIssue", "action"] as const;

// Billing-return params: not commands. useCheckoutReturn owns their strips.
// The OAuth/Stripe-return params join them for the same reason: the provider
// redirects back carrying ?connected=/?error=/?stripe*=, ConnectionsPanel turns
// them into a one-shot banner and strips them, and treating them as command
// params would make that strip churn the reconciler's command signature.
export const NON_COMMAND_PARAMS = [
  "checkout",
  "subscription",
  "provider",
  "connected",
  "error",
  "stripe",
  "stripe_connect",
  "stripe_error",
] as const;

const TEAMS_LIST_KEYS: Readonly<Record<string, string>> = {
  dashboard: "Home",
  agents: "Agents",
  inbox: "Inbox",
  issues: "Tasks",
  routines: "Automations",
  artifacts: "Library",
  accounts: "Accounts",
  search: "Search",
  settings: "Team Settings",
  // The one ledger page. The four keys it absorbed live in
  // RETIRED_PANEL_ALIASES, not here — one page, one panel id.
  // CONTROL_KEYS.usage stays exactly as it is: `?panel=usage` links are
  // already in the wild, and Money's Agent spend section is a different,
  // smaller surface than that panel.
  money: "Money",
  // Shells with named exits (owner ruling 2026-09-01): the map is complete
  // before every rail is. Keys are permanent the moment they ship — mobile
  // builds `?panel=teams-<key>` as a string.
  publish: "Publish",
  seo: "SEO & AEO",
  ads: "Ads",
  support: "Support",
  identity: "Identity",
  "domain-site": "Domain & Site",
  compliance: "Compliance",
  contracts: "Contracts",
  leads: "Leads",
};

const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Automation",
  "project-detail": "Project",
  "goal-detail": "Goal",
  "run-detail": "Run",
};

// Legacy control keys kept for URL grammar parity (ChatErrorCard links to
// ?panel=llm; the agent detail view to ?panel=agents&agent=…). They open as
// dock panels but are NOT in the sidebar — /settings remains their home.
const CONTROL_KEYS: Readonly<Record<string, string>> = {
  overview: "Overview",
  usage: "Usage",
  agents: "Agents",
  skills: "Skills",
  llm: "Model & Credits",
  integrations: "Integrations",
  // Third-party connect/disconnect. Unlike its neighbours this one IS in the
  // sidebar — the workspace rail, under Browser and Files — and /settings no
  // longer carries it. The /settings/connections route survives as a redirector
  // because the backend's Stripe return URL points at it.
  connections: "Connections",
};

export const PANEL_COMPONENT_KEYS: ReadonlySet<string> = new Set([
  "chat",
  "teams",
  "control",
  "file-viewer",
  "browser",
  "missing",
]);

// Key-map membership must be OWN-property only (Object.hasOwn): the `in`
// operator walks the prototype chain, so Object.prototype keys
// ("constructor", "valueOf", …) in a crafted ?panel URL would pass validation
// and render inherited functions as titles/components instead of rejecting to
// null.

export function resolvePanelAlias(panelKey: string): string {
  return Object.hasOwn(RETIRED_PANEL_ALIASES, panelKey) ? RETIRED_PANEL_ALIASES[panelKey] : panelKey;
}

export function chatPanelId(agentId: string): string {
  return `chat:${agentId}`;
}

export const MESSAGES_PANEL_ID = chatPanelId(DEFAULT_AGENT_SENTINEL);

export function describePanel(
  panelKey: string,
  params: Record<string, string>,
): PanelDescriptor | null {
  const key = resolvePanelAlias(panelKey);

  if (key === "chat") {
    const agentId = params.agent ?? params.agentId;
    if (agentId) {
      return {
        panelId: MESSAGES_PANEL_ID,
        component: "chat",
        title: "Messages",
        params: { agentId },
        renderer: "always",
      };
    }
    // Room permalinks (?panel=chat&room=…&message=…): `message` is a durable
    // param here, not a one-shot — the reconciler delivers chat params
    // straight from descriptor.params (never via oneShots), see Ruling 3 in
    // task-7a's report.
    if (params.room) {
      return {
        panelId: MESSAGES_PANEL_ID,
        component: "chat",
        title: "Messages",
        params: { roomId: params.room, ...(params.session ? { sessionId: params.session } : params.message ? { messageId: params.message } : {}) },
        renderer: "always",
      };
    }
    return null;
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
    // Singleton, no params: an owner has ONE AgentCore Browser profile, and the
    // panel watches whichever session is live on it — so a second open focuses
    // the existing panel rather than double-viewing the same stream.
    return { panelId: "browser", component: "browser", title: "Browser", params: {} };
  }

  if (key === "agent-inspector") {
    const p: Record<string, string> = { controlKey: "agent-inspector" };
    if (params.agent) p.agentId = params.agent;
    if (params.inspectorTab) p.initialTab = params.inspectorTab;
    return { panelId: "agent-inspector", component: "control", title: "Agent Inspector", params: p };
  }

  // Retired (task-8): a session is a view inside Messages, never its own dock
  // tab — every "session" URL/link retargets the same chat descriptor a room
  // permalink with ?session= already produces.
  if (key === "session") {
    if (!params.room || !params.session) return null;
    return describePanel("chat", { room: params.room, session: params.session });
  }

  if (key.startsWith("teams-")) {
    const teamsKey = key.slice("teams-".length);
    if (Object.hasOwn(TEAMS_DETAIL_KEYS, teamsKey)) {
      const detailId = params.detailId;
      if (!detailId) return null;
      return {
        panelId: `${key}:${detailId}`,
        component: "teams",
        title: TEAMS_DETAIL_KEYS[teamsKey],
        params: { teamsKey, detailId },
      };
    }
    if (Object.hasOwn(TEAMS_LIST_KEYS, teamsKey)) {
      const p: Record<string, string> = { teamsKey };
      // Durable filters ride along; one-shots are handled by the reconciler
      // at open time and never enter these params.
      if (teamsKey === "issues") {
        if (params.agent) p.agent = params.agent;
        // Goals and Projects absorbed into Tasks as groupings, so the cut a
        // link meant survives only if it is read off the RAW key — same
        // synthesis, same reason, as `money` and `inbox`.
        if (panelKey === "teams-goals") p.groupBy = "goal";
        else if (panelKey === "teams-projects") p.groupBy = "project";
        else if (params.groupBy && TASKS_GROUP_BY.has(params.groupBy)) {
          p.groupBy = params.groupBy;
        }
      }
      // Agents absorbed the org chart as its second view, so the view a link
      // meant survives only if it is read off the RAW key — same synthesis,
      // same reason, as `money` and `inbox`.
      if (teamsKey === "agents") {
        if (panelKey === "teams-org-chart") p.view = "chart";
        else if (params.view && AGENTS_VIEWS.has(params.view)) p.view = params.view;
      }
      // Inbox absorbed Activity, so the band a link meant survives only if it
      // is read off the RAW key — same synthesis, same reason, as `money`.
      if (teamsKey === "inbox") {
        if (panelKey === "teams-activity") p.section = "recently";
        else if (params.section && INBOX_SECTIONS.has(params.section)) p.section = params.section;
        // Bookmarks predating the pill tabs still carry ?tab=approvals; that
        // band is now "Needs you". Every other old tab value named an issue
        // list, which lives on Tasks — those land at the top of Inbox.
        else if (params.tab === "approvals") p.section = "needs-you";
      }
      // ?day scopes the Payments section to one UTC day — the hero bar on Home links here.
      if (teamsKey === "money" && params.day) p.day = params.day;
      // The four absorbed keys (teams-payments/products/customers/spend) alias
      // to teams-money, so the section they meant survives only if it is read
      // off the RAW key — same synthesis, same reason, as `agents` above.
      if (teamsKey === "money") {
        const raw = panelKey.startsWith("teams-") ? panelKey.slice("teams-".length) : panelKey;
        if (MONEY_SECTIONS.has(raw)) p.section = raw;
        else if (params.section && MONEY_SECTIONS.has(params.section)) p.section = params.section;
      }
      return { panelId: key, component: "teams", title: TEAMS_LIST_KEYS[teamsKey], params: p };
    }
    return null;
  }

  if (Object.hasOwn(CONTROL_KEYS, key)) {
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
  if (panelId.startsWith("chat:")) return ["panel", "agent", "agentId", "room", "message", "session"];
  if (panelId === "file-viewer") return ["panel", "path", "agent"];
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === "teams-agents") return ["panel", "view"];
  if (panelId === "teams-issues") return ["panel", "agent", "groupBy"];
  if (panelId === "teams-inbox") return ["panel", "section"];
  if (panelId === "teams-money") return ["panel", "day", "section"];
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
  // roomId/messageId included alongside agentId/agentSelection: an agent open
  // must clear a stale room target and vice versa, or the two grammars fight
  // (see openMessagePanel / openChatPanel in workspaceController.ts).
  if (panelId === MESSAGES_PANEL_ID) return ["agentId", "agentSelection", "roomId", "messageId", "sessionId", "sessionIntent"];
  if (panelId === "teams-agents") return ["view"];
  if (panelId === "teams-issues") return ["agent", "groupBy"];
  if (panelId === "teams-inbox") return ["section"];
  if (panelId === "teams-money") return ["day", "section"];
  if (panelId === "file-viewer") return ["path", "agent"];
  if (panelId === "agent-inspector" || panelId === "control:agents") {
    return ["agentId", "initialTab"];
  }
  return [];
}
