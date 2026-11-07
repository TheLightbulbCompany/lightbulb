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
  // Money is Settings › Business; describePanel reads the RAW key to pick that section.
  "teams-payments": "settings",
  "teams-products": "settings",
  "teams-customers": "settings",
  "teams-spend": "settings",
  "teams-activity": "activity",
  // The Work › Threads overview folded into Home (2026-11-05): Home lists the same threads, grouped.
  "teams-threads": "activity",
  // The org chart was a view of the Team roster.
  "teams-org-chart": "teams-agents",
  // The Support placeholder (later Cases).
  "teams-support": "activity",
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
  // The Paperclip board is gone (Phase 1 task 7): its pages and their old links open Activity, where
  // tasks live. Work dissolved earlier (UX slice 4): Automations open Activity's Scheduled group, Money is
  // Settings › Business, Library is Creations (the canvas panel; slice 5).
  "teams-issues": "activity",
  "teams-goals": "activity",
  "teams-projects": "activity",
  "teams-cases": "activity",
  "teams-search": "activity",
  "teams-routines": "activity",
  "teams-issue-detail": "activity",
  "teams-routine-detail": "activity",
  "teams-project-detail": "activity",
  "teams-goal-detail": "activity",
  "teams-case-detail": "activity",
  "teams-run-detail": "activity",
  "teams-approval-detail": "activity",
  "teams-money": "settings",
  "teams-artifacts": "canvas",
  // The rail writes `panel=creations` (W13); the panel id stays "canvas", so an old `panel=canvas` link still opens it.
  creations: "canvas",
};

// The old Money page's sections: a link to any of them opens Settings › Business.
const MONEY_SECTIONS: ReadonlySet<string> = new Set([
  "payments",
  "customers",
  "products",
  "spend",
]);

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
    // Where "‹" and Escape return ("task:<id>", "files"): in the URL, so the reconcile of the pushed URL (and a
    // reload) keeps it. It was a one-shot, and that reconcile's re-open cleared it: Escape closed the pane (W14-5).
    if (params.back && /^(task:[\w-]+|tasks|files|scheduled|creations|chat)$/.test(params.back)) p.back = params.back;
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
    return { panelId: "canvas", component: "canvas", title: "Creations", params: p };
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
    if (Object.hasOwn(TEAMS_LIST_KEYS, teamsKey)) {
      return { panelId: key, component: "teams", title: TEAMS_LIST_KEYS[teamsKey], params: { teamsKey } };
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
  if (panelId === "artifact") return ["panel", ...ARTIFACT_PARAM_KEYS, "back"];
  if (panelId === "task-detail") return ["panel", "taskId"];
  if (panelId === "agent-inspector") return ["panel", "agent", "inspectorTab"];
  if (panelId === "control:agents") return ["panel", "agent"];
  if (panelId === "activity") return ["panel", "filter", "focus", "section"];
  if (panelId === "settings") return ["panel", "section"];
  if (panelId === "automation") return ["panel", "id"];
  if (panelId === "canvas") return ["panel", "app"];
  if (panelId === "mail") return ["panel", "item"];
  if (panelId === "ideas") return ["panel", "agent", "idea"];
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
  if (panelId === "activity") return ["filter", "focus", "section"];
  if (panelId === "settings") return ["section"];
  if (panelId === "automation") return ["id"];
  if (panelId === "canvas") return ["app"];
  if (panelId === "mail") return ["item"];
  if (panelId === "ideas") return ["agent", "idea"];
  if (panelId === "file-viewer") return ["path", "agent"];
  // `back` (openOutput): a file opened from elsewhere must not keep the last one's way back.
  // `heading` is the shown doc's own (ArtifactDockPanel): the next file reads its own.
  if (panelId === "artifact") return [...ARTIFACT_PARAM_KEYS, "back", "heading", "headingOf"];
  if (panelId === "task-detail") return ["taskId"];
  if (panelId === "agent-inspector" || panelId === "control:agents") {
    return ["agentId", "initialTab"];
  }
  return [];
}

/** Which slot a descriptor opens in: lists and feeds are places, one single thing is the right pane. */
export function opensInPane(descriptor: PanelDescriptor): boolean {
  return descriptor.component === "file-viewer" || descriptor.component === "browser"
    || descriptor.component === "mail" || descriptor.component === "control" || descriptor.component === "ideas"
    || descriptor.component === "artifact" || descriptor.component === "task" || descriptor.component === "automation";
}
