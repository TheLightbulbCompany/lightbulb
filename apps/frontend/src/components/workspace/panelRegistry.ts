// Single source of panel identity for the workspace (one main view per place,
// one right pane). The URL reconciler, the controller and the shell's
// components map all derive from this file — nothing else may invent a
// panel id or component name.

import { ARTIFACT_PARAM_KEYS, parseArtifactSource } from "./panels/artifactSource";

export type PanelKind = "chat" | "teams" | "control" | "file-viewer" | "browser" | "activity" | "home" | "canvas" | "mail" | "ideas" | "artifact" | "task" | "settings" | "automation";

export interface PanelDescriptor {
  panelId: string;
  component: PanelKind;
  title: string;
  params: Record<string, string>;
}

export const DEFAULT_AGENT_SENTINEL = "__default__";

// Retired panel keys still in old links, mobile builds and notifications: each
// opens its successor (the URL resolver maps them; nothing is ever saved under them).
export const RETIRED_PANEL_ALIASES: Readonly<Record<string, string>> = {
  // The Inbox tab folded into Activity's For you (2026-10-15): it, and the keys it had
  // absorbed, open Activity.
  "teams-inbox": "activity",
  "teams-approvals": "activity",
  // Home is no longer a destination (stage-2 rail trim): it, and the keys it
  // had absorbed, open the viewer's Activity feed.
  "teams-dashboard": "activity",
  "teams-skills": "activity",
  "teams-costs": "activity",
  // Money is ONE page, and Payments/Products/Customers/Spend are its sections.
  // They alias to its key so Work opens ONE Money page no matter which of the
  // five keys a link carries. describePanel synthesizes `section` from the RAW key
  // below.
  "teams-payments": "teams-money",
  "teams-products": "teams-money",
  "teams-customers": "teams-money",
  "teams-spend": "teams-money",
  "teams-activity": "activity",
  // The Work › Threads overview folded into Home (2026-11-05): Home lists the same threads, grouped.
  "teams-threads": "activity",
  // NOTE: `teams-goals` and `teams-projects` are deliberately NOT aliased —
  // they are their own panels (GoalsPanel / ProjectsListPanel, TEAMS_LIST_KEYS
  // below), same as stock Paperclip's Goals and Projects pages. They used to
  // alias onto `teams-issues` pre-grouped; lifting that alias (owner ruling
  // 2026-09-24) is what makes them reachable again.

  // The org chart is the SAME roster read, drawn as a tree — a view of the
  // Agents panel, not a page of its own. Aliased for the same reason as the
  // four Money keys. describePanel synthesizes
  // `view` from the RAW key below.
  "teams-org-chart": "teams-agents",
  // The Support placeholder was removed (owner ruling) when Cases landed; old
  // links and saved layouts open Cases instead of an unknown panel.
  "teams-support": "teams-cases",
  // Deleted tabs (2026-10-14, stage 1 of the product simplification): an old link or a
  // mobile-built `?panel=teams-<key>` opens Home, never a dead-end page.
  "teams-publish": "home",
  "teams-seo": "home",
  "teams-ads": "home",
  "teams-identity": "home",
  "teams-domain-site": "home",
  "teams-compliance": "home",
  "teams-contracts": "home",
  "teams-leads": "home",
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
// stripped from the URL, cleared after consumption.
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
  agents: "Team",
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
};

// The Work place: these task-board pages share ONE main view, "Work", and a tab
// strip inside it switches between them (panels/WorkTabs.tsx).
export const WORK_PANEL_ID = "teams-work";
// Agents left Work for its own place, Team (2026-11-05): the owner hires and manages specialists there.
export const WORK_TEAMS_KEYS = ["issues", "projects", "goals", "routines", "cases", "money"] as const;
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

/** A /settings URL as the Settings place in the shell (super-agent): `?panel=` becomes `section`, every other param
 *  (OAuth/Stripe returns, ?action=) kept. */
export function shellSettingsHref(settingsHref: string): string {
  const next = new URLSearchParams(settingsHref.split("?")[1] ?? "");
  const section = next.get("panel");
  next.set("panel", "settings");
  next.delete("section");
  if (section) next.set("section", section);
  return `/chat?${next.toString()}`;
}

// Key-map membership must be OWN-property only (Object.hasOwn): the `in`
// operator walks the prototype chain, so Object.prototype keys
// ("constructor", "valueOf", …) in a crafted ?panel URL would pass validation
// and render inherited functions as titles/components instead of rejecting to
// null.

// Super-agent (UX slice 4): Work dissolved. Its Automations open Activity's Scheduled group, and Money is
// Settings › Business. Library opens Creations (slice 5). The switch is per workspace, so the shell sets it (setSuperAgentAliases) and a
// switch-off workspace keeps every key exactly as before.
const SUPER_AGENT_ALIASES: Readonly<Record<string, string>> = {
  "teams-routines": "activity",
  "teams-money": "settings",
  // UX slice 5: Creations (the canvas panel) replaces Library.
  "teams-artifacts": "canvas",
};
let _superAgentAliases = false;
export function setSuperAgentAliases(on: boolean): void {
  _superAgentAliases = on;
}

export function resolvePanelAlias(panelKey: string): string {
  const key = Object.hasOwn(RETIRED_PANEL_ALIASES, panelKey) ? RETIRED_PANEL_ALIASES[panelKey] : panelKey;
  return _superAgentAliases && Object.hasOwn(SUPER_AGENT_ALIASES, key) ? SUPER_AGENT_ALIASES[key] : key;
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
        title: "Chat",
        params: { agentId }
      };
    }
    // Room permalinks (?panel=chat&room=…&message=…): `message` is a durable
    // param here, not a one-shot — the reconciler delivers chat params
    // straight from descriptor.params (never via oneShots), see Ruling 3 in
    // task-7a's report.
    // A session is workspace-scoped: a `room` riding along (old links) is ignored.
    if (params.session) {
      // `seq`: open the session's conversation at that turn (a session search hit).
      return { panelId: MESSAGES_PANEL_ID, component: "chat", title: "Chat", params: { sessionId: params.session, ...(params.seq ? { seq: params.seq } : {}) } };
    }
    if (params.room) {
      return {
        panelId: MESSAGES_PANEL_ID,
        component: "chat",
        title: "Chat",
        params: { roomId: params.room, ...(params.message ? { messageId: params.message } : {}) }
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

  if (key === "artifact") {
    const p: Record<string, string> = {};
    for (const k of ARTIFACT_PARAM_KEYS) if (params[k]) p[k] = params[k];
    if (!parseArtifactSource(p)) return null;
    return { panelId: "artifact", component: "artifact", title: p.name ?? p.ident ?? "File", params: p };
  }

  // One super-agent task (TaskPane). Only reachable while the switch is on: nothing links here otherwise.
  if (key === "task-detail") {
    if (!params.taskId) return null;
    return { panelId: "task-detail", component: "task", title: "Task", params: { taskId: params.taskId } };
  }

  if (key === "browser") {
    // Singleton, no params: an owner has ONE AgentCore Browser profile, and the
    // pane watches whichever session is live on it.
    return { panelId: "browser", component: "browser", title: "Browser", params: {} };
  }

  // Activity: the viewer's requests (super-agent: Needs you, mentions, Ideas, the feed, Working/Done/Closed; it was
  // Home until 6 Nov 2026), else their Activity feed (mentions, DMs, thread replies, what Paperclip asks of them).
  // Singleton: one per viewer. The old company-wide view stays the Teams Inbox, reachable from inside the feed.
  // `filter` opens a chip (the Tasks header's "Needs you"); `focus` is the item a
  // notification points at (an approval), scrolled to and highlighted.
  if (key === "activity") {
    const p: Record<string, string> = {};
    if (params.filter) p.filter = params.filter;
    if (params.focus) p.focus = params.focus;
    // `section=scheduled` opens the Scheduled group (super-agent; an old Automations link lands there).
    if (panelKey === "teams-routines" || params.section === "scheduled") p.section = "scheduled";
    return { panelId: "activity", component: "activity", title: "Activity", params: p };
  }

  // Home (super-agent, 6 Nov 2026): the chats and the project spaces. Singleton, no params.
  if (key === "home") return { panelId: "home", component: "home", title: "Home", params: {} };

  // Settings inside the shell (super-agent, UX slice 4): `section` is the nav row. An old Money link (any of
  // its five keys) opens Business.
  if (key === "settings") {
    const raw = panelKey.startsWith("teams-") ? panelKey.slice("teams-".length) : "";
    const section = raw === "money" || MONEY_SECTIONS.has(raw) ? "business" : params.section;
    return { panelId: "settings", component: "settings", title: "Settings", params: section ? { section } : {} };
  }

  // One scheduled automation in the pane (super-agent). `id` is the automation's id.
  if (key === "automation") {
    if (!params.id) return null;
    return { panelId: "automation", component: "automation", title: "Scheduled", params: { id: params.id } };
  }

  // Apps the agents built; `app` opens one inside the pane.
  if (key === "canvas") {
    const p: Record<string, string> = {};
    if (params.app) p.app = params.app;
    return { panelId: "canvas", component: "canvas", title: _superAgentAliases ? "Creations" : "Canvas", params: p };
  }

  // The workspace's shared agent mailbox: mail in and out, meetings and their
  // transcripts. Singleton; `item` is the open row (`mail:<key>` or
  // `meeting:<id>:<start_at>`).
  if (key === "mail") {
    return { panelId: "mail", component: "mail", title: "Mail", params: params.item ? { item: params.item } : {} };
  }

  // Ideas (spec 2026-10-14 §4.4), opened by the 💡 in a conversation header. Singleton;
  // `agent` puts that teammate's ideas first (opened from its DM).
  if (key === "ideas") {
    // `idea` opens that idea's detail (the welcome's row links here).
    const p: Record<string, string> = {};
    if (params.agent) p.agent = params.agent;
    if (params.idea) p.idea = params.idea;
    return { panelId: "ideas", component: "ideas", title: "Ideas", params: p };
  }

  if (key === "agent-inspector") {
    const p: Record<string, string> = { controlKey: "agent-inspector" };
    if (params.agent) p.agentId = params.agent;
    if (params.inspectorTab) p.initialTab = params.inspectorTab;
    return { panelId: "agent-inspector", component: "control", title: "Specialist", params: p };
  }

  // Retired (task-8): a session is a view inside Messages, never its own
  // panel — every "session" URL/link retargets the same chat descriptor a room
  // permalink with ?session= already produces.
  if (key === "session") {
    if (!params.session) return null;
    return describePanel("chat", { session: params.session, ...(params.seq ? { seq: params.seq } : {}) });
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
      // ?day scopes the Payments section to one UTC day (Money's biggest-day link).
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
  if (panelId.startsWith("chat:")) return ["panel", "agent", "agentId", "room", "message", "session", "seq"];
  if (panelId === "file-viewer") return ["panel", "path", "agent"];
  if (panelId === "artifact") return ["panel", ...ARTIFACT_PARAM_KEYS];
  if (panelId === "task-detail") return ["panel", "taskId"];
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === WORK_PANEL_ID) return ["panel", "agent", "groupBy", "view", "day", "section"];
  if (panelId === "teams-agents") return ["panel", "view"];
  if (panelId === "activity") return ["panel", "filter", "focus", "section"];
  if (panelId === "settings") return ["panel", "section"];
  if (panelId === "automation") return ["panel", "id"];
  if (panelId === "canvas") return ["panel", "app"];
  if (panelId === "mail") return ["panel", "item"];
  if (panelId === "ideas") return ["panel", "agent", "idea"];
  if (/^teams-(issue|routine|project|goal|case|run)-detail:/.test(panelId)) return ["panel", "detailId"];
  return ["panel"];
}

// PANEL-param keys (descriptor.params space — agentId/initialTab, not the URL
// grammar's agent/inspectorTab) that a plain re-open of an already-open panel
// must CLEAR when the fresh descriptor doesn't carry them: a re-open merges,
// so without the clear a stale durable param (an agent filter, an inspector
// tab) survives an unfiltered navigation — breaking the mobile invariant that
// plain panel nav drops stale state.
export function clearablePanelParamKeys(panelId: string): string[] {
  // roomId/messageId included alongside agentId/agentSelection: an agent open
  // must clear a stale room target and vice versa, or the two grammars fight
  // (see openMessagePanel / openChatPanel in workspaceController.ts).
  if (panelId === MESSAGES_PANEL_ID) return ["agentId", "agentSelection", "roomId", "messageId", "sessionId", "seq", "sessionIntent"];
  if (panelId === WORK_PANEL_ID) return ["agent", "groupBy", "view", "day", "section"];
  if (panelId === "teams-agents") return ["view"];
  if (panelId === "activity") return ["filter", "focus", "section"];
  if (panelId === "settings") return ["section"];
  if (panelId === "automation") return ["id"];
  if (panelId === "canvas") return ["app"];
  if (panelId === "mail") return ["item"];
  if (panelId === "ideas") return ["agent", "idea"];
  if (panelId === "file-viewer") return ["path", "agent"];
  if (panelId === "artifact") return [...ARTIFACT_PARAM_KEYS];
  if (panelId === "task-detail") return ["taskId"];
  if (panelId === "agent-inspector" || panelId === "control:agents") {
    return ["agentId", "initialTab"];
  }
  return [];
}

/** Which slot a descriptor opens in: lists and feeds are places, one single thing is the right pane. */
export function opensInPane(descriptor: PanelDescriptor): boolean {
  if (descriptor.component === "teams") return !!descriptor.params.detailId;
  return descriptor.component === "file-viewer" || descriptor.component === "browser"
    || descriptor.component === "mail" || descriptor.component === "control" || descriptor.component === "ideas"
    || descriptor.component === "artifact" || descriptor.component === "task" || descriptor.component === "automation";
}
