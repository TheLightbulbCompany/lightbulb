// Single source of panel identity for the dockview workspace. The URL
// reconciler, the controller, the persistence sanitizer, and the shell's
// components map all derive from this file — nothing else may invent a
// panel id or component name.

export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "browser" | "activity" | "mail" | "missing";

export interface PanelDescriptor {
  panelId: string;
  component: PanelKind;
  title: string;
  params: Record<string, string>;
  renderer?: "always";
}

export const DEFAULT_AGENT_SENTINEL = "__default__";

// Merged from ControlPanelRouter and TeamsPanelRouter
// (RETIRED_PANEL_ALIASES) — applied replace-only in the URL resolver so a
// Back landing on a retired key canonicalizes in place instead of pushing
// (the old ControlPanelRouter remap pushed, trapping Back on ?panel=credits).
export const RETIRED_PANEL_ALIASES: Readonly<Record<string, string>> = {
  "teams-approvals": "teams-inbox",
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
  // NOTE: `teams-goals` and `teams-projects` are deliberately NOT aliased —
  // they are their own panels (GoalsPanel / ProjectsListPanel, TEAMS_LIST_KEYS
  // below), same as stock Paperclip's Goals and Projects pages. They used to
  // alias onto `teams-issues` pre-grouped; lifting that alias (owner ruling
  // 2026-09-24) is what makes them reachable again.

  // The org chart is the SAME roster read, drawn as a tree — a view of the
  // Agents panel, not a page of its own. Aliased for the same reason as the
  // four Money keys: the dock dedupes by panel id, so a router-level mapping
  // would open two tabs both titled "Agents". describePanel synthesizes
  // `view` from the RAW key below.
  "teams-org-chart": "teams-agents",
  // The Support placeholder was removed (owner ruling) when Cases landed; old
  // links and saved layouts open Cases instead of an unknown panel.
  "teams-support": "teams-cases",
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
  // Reachable again as their own panels — see the note above
  // RETIRED_PANEL_ALIASES. GoalDetailPage/ProjectsListPanel's leaveToList
  // already targeted these keys (WorkspaceShell.DETAIL_KEY_BY_LIST_KEY).
  goals: "Goals",
  projects: "Projects",
  // Ported from stock Paperclip's Cases page. Replaces the Support placeholder
  // (`teams-support` aliases here).
  cases: "Cases",
  routines: "Automations",
  artifacts: "Library",
  search: "Search",
  // The one ledger page. The four keys it absorbed live in
  // RETIRED_PANEL_ALIASES, not here — one page, one panel id.
  money: "Money",
  // Deleted tabs (2026-10-14, stage 1 of the product simplification). The keys
  // stay so an old link or a mobile-built `?panel=teams-<key>` still opens a
  // titled tab that shows the router's "Unknown panel" line instead of nothing.
  publish: "Publish",
  seo: "SEO & AEO",
  ads: "Ads",
  identity: "Identity",
  "domain-site": "Domain & Site",
  compliance: "Compliance",
  contracts: "Contracts",
  leads: "Leads",
};

// The Work page: these task-board pages share ONE dock tab, "Work", and a tab
// strip inside it switches between them (panels/WorkTabs.tsx). Opening any of
// them re-targets that tab instead of adding a tab per page.
export const WORK_PANEL_ID = "teams-work";
export const WORK_TEAMS_KEYS = ["issues", "projects", "goals", "routines", "cases", "agents", "money"] as const;
const WORK_KEY_SET: ReadonlySet<string> = new Set(WORK_TEAMS_KEYS);
export function isWorkTeamsKey(teamsKey: string): boolean {
  return WORK_KEY_SET.has(teamsKey);
}

const TEAMS_DETAIL_KEYS: Readonly<Record<string, string>> = {
  "issue-detail": "Task",
  "routine-detail": "Automation",
  "project-detail": "Project",
  "goal-detail": "Goal",
  "case-detail": "Case",
  "run-detail": "Run",
  "approval-detail": "Approval",
};

// The agent detail view (?panel=agents&agent=…). The other control panels
// moved to the Settings page — see SETTINGS_SECTION_FOR below.
const CONTROL_KEYS: Readonly<Record<string, string>> = {
  agents: "Agents",
};

// Panel keys the one Settings page took (product simplification, 2026-10-14),
// mapped to the Settings section that holds them now ("" = the page itself:
// Overview was a status page, not a setting). Old links, mobile "Manage on web"
// links and the backend's action_href still carry these keys, so /chat sends
// them on instead of opening a dead tab (SettingsRedirect, app/chat/page.tsx).
const SETTINGS_SECTION_FOR: Readonly<Record<string, string>> = {
  overview: "",
  usage: "usage",
  skills: "skills",
  integrations: "connections",
  connections: "connections",
  llm: "providers",
  credits: "providers",
  "teams-settings": "workspace",
  "teams-members": "members",
  "teams-accounts": "connections",
};

/** The Settings URL a `?panel=` search now belongs to, every other param kept
 *  (OAuth/Stripe returns, `?provider=`, `?action=change-provider`); null when
 *  the search names no key Settings took. */
export function settingsHrefFor(search: string): string | null {
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  const key = params.get("panel");
  if (key === null || !Object.hasOwn(SETTINGS_SECTION_FOR, key)) return null;
  const section = SETTINGS_SECTION_FOR[key];
  const out = new URLSearchParams(section ? { panel: section } : {});
  params.forEach((v, k) => {
    if (k !== "panel") out.append(k, v);
  });
  const qs = out.toString();
  return qs ? `/settings?${qs}` : "/settings";
}

export const PANEL_COMPONENT_KEYS: ReadonlySet<string> = new Set([
  "chat",
  "teams",
  "control",
  "file-viewer",
  "browser",
  "activity",
  "mail",
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
    // A session is workspace-scoped: a `room` riding along (old links) is ignored.
    if (params.session) {
      return { panelId: MESSAGES_PANEL_ID, component: "chat", title: "Messages", params: { sessionId: params.session }, renderer: "always" };
    }
    if (params.room) {
      return {
        panelId: MESSAGES_PANEL_ID,
        component: "chat",
        title: "Messages",
        params: { roomId: params.room, ...(params.message ? { messageId: params.message } : {}) },
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
    // "always": a tab behind Messages stays in the document, so its live view keeps its
    // DCV display instead of reconnecting (or failing to find its element) on every switch.
    return { panelId: "browser", component: "browser", title: "Browser", params: {}, renderer: "always" };
  }

  // The viewer's Activity feed (mentions, DMs, thread replies, what Paperclip asks of
  // them). Singleton: one feed per viewer. The old company-wide view stays the Teams
  // Inbox, reachable from inside the feed.
  // `filter` opens a chip (the Tasks header's "Needs you"); `focus` is the item a
  // notification points at (an approval), scrolled to and highlighted.
  if (key === "activity") {
    const p: Record<string, string> = {};
    if (params.filter) p.filter = params.filter;
    if (params.focus) p.focus = params.focus;
    return { panelId: "activity", component: "activity", title: "Activity", params: p };
  }

  // The workspace's shared agent mailbox: mail in and out, meetings and their
  // transcripts. Singleton; `item` is the open row (`mail:<key>` or
  // `meeting:<id>:<start_at>`).
  if (key === "mail") {
    return { panelId: "mail", component: "mail", title: "Mail", params: params.item ? { item: params.item } : {} };
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
    if (!params.session) return null;
    return describePanel("chat", { session: params.session });
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
        // "goal"/"project" remain valid explicit ?groupBy= values here — the
        // Tasks page's own grouping menu (TasksList.GROUP_OPTIONS) still
        // offers them. Only the retired-alias RAW-key synthesis is gone
        // (teams-goals/teams-projects are their own panels now).
        if (params.groupBy && TASKS_GROUP_BY.has(params.groupBy)) {
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
      if (isWorkTeamsKey(teamsKey)) return { panelId: WORK_PANEL_ID, component: "teams", title: "Work", params: p };
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
  if (panelId === WORK_PANEL_ID) return ["panel", "agent", "groupBy", "view", "day", "section"];
  if (panelId === "teams-inbox") return ["panel", "section"];
  if (panelId === "activity") return ["panel", "filter", "focus"];
  if (panelId === "mail") return ["panel", "item"];
  if (/^teams-(issue|routine|project|goal|case|run)-detail:/.test(panelId)) return ["panel", "detailId"];
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
  if (panelId === WORK_PANEL_ID) return ["agent", "groupBy", "view", "day", "section"];
  if (panelId === "teams-inbox") return ["section"];
  if (panelId === "activity") return ["filter", "focus"];
  if (panelId === "mail") return ["item"];
  if (panelId === "file-viewer") return ["path", "agent"];
  if (panelId === "agent-inspector" || panelId === "control:agents") {
    return ["agentId", "initialTab"];
  }
  return [];
}
