// Persisted dockview layout blob. Template: desktopPermissions.ts — version
// in the key AND the payload, strict validation, null-on-any-failure reads,
// swallow-on-failure writes (private browsing / quota → session just isn't
// persistent). NEVER call localStorage.clear() here: a key===null storage
// event is treated as a permissions event by the gateway listeners.
import type { SerializedDockview } from "dockview-react";
import {
  PANEL_COMPONENT_KEYS,
  ONE_SHOT_PARAMS,
  RETIRED_PANEL_ALIASES,
  DEFAULT_AGENT_SENTINEL,
  MESSAGES_PANEL_ID,
  describePanel,
} from "./panelRegistry";

// v3: the shell is tabs-only — splitting is gone, so a persisted v2 blob is a
// saved three-pane SPLIT that would restore verbatim and hide the change from
// every existing owner. The bump orphans them so everyone lands on the new
// tabbed default once; readLayout drops the dead key so it does not sit in
// localStorage forever.
//
// (v2 was itself the bump that put Browser, not Dashboard, in the right-top
// slot.)
const KEY_PREFIX = "isol8.workspace.layout.v3";
// Every superseded key, not just the immediately previous one: v1 blobs would
// otherwise be stranded forever in the storage of anyone who last loaded
// before v2 shipped.
const LEGACY_KEY_PREFIXES = [
  "isol8.workspace.layout.v2",
  "isol8.workspace.layout.v1",
] as const;

// Params that may live on a panel while it's open but must NOT survive into
// the persisted blob. initialTab is a navigation intent (the Get Started
// "Connect a channel" CTA), not view state — persisting it would land the
// inspector on that tab on EVERY restore, forever. Same for the inbox's
// deep-linked tab: the user's chosen tab lives in localStorage, and a
// persisted tab param would silently override it on every reload.
const PERSIST_ONLY_STRIP = ["initialTab", "tab"] as const;

interface StoredLayout {
  version: 3;
  savedAt: string;
  layout: SerializedDockview;
}

// RETIRED_PANEL_ALIASES is otherwise applied replace-only to URL commands
// (see panelRegistry.ts) — a saved dockview layout restores panel ids/params
// straight from localStorage, bypassing that resolver entirely. A layout
// saved before a panel was retired still carries the old id, so it must be
// migrated here too. Two id shapes show up in practice: teams-* aliases are
// bare panel ids (panelId === registry key), non-teams aliases (credits→llm)
// are control-panel ids carrying the `control:` prefix.
//
// A THIRD shape is possible and handled: a `control:`-prefixed persisted id
// whose alias target crosses into the teams- namespace. That must flip
// `contentComponent` from "control" to "teams" too — the two are different
// top-level React components (ControlDockPanel vs TeamsDockPanel) keyed off
// different param shapes (controlKey vs teamsKey) — so `component` is
// signalled back to the caller. No alias in the map crosses today (`dreams`
// did until it went back to being its own control panel).
function migrateRetiredPanelId(
  id: string,
): { id: string; teamsKey?: string; controlKey?: string; component?: "teams" } | null {
  if (Object.hasOwn(RETIRED_PANEL_ALIASES, id)) {
    const target = RETIRED_PANEL_ALIASES[id];
    if (target.startsWith("teams-")) {
      // Hardening: every existing bare teams-* alias is already
      // contentComponent "teams", so this is a no-op for them today — but
      // stating it explicitly (rather than relying on the `component ??
      // contentComponent` fallback below) means a bare id retired FROM a
      // control panel straight into teams- would still flip correctly.
      return { id: target, teamsKey: target.slice("teams-".length), component: "teams" };
    }
    return null;
  }
  if (id.startsWith("control:")) {
    const key = id.slice("control:".length);
    if (Object.hasOwn(RETIRED_PANEL_ALIASES, key)) {
      const target = RETIRED_PANEL_ALIASES[key];
      if (target.startsWith("teams-")) {
        return { id: target, teamsKey: target.slice("teams-".length), component: "teams" };
      }
      return { id: `control:${target}`, controlKey: target };
    }
  }
  return null;
}

type GridNode = SerializedDockview["grid"]["root"];

function activeViewInGroup(node: GridNode, groupId: string | undefined): string | undefined {
  if (node.type === "branch") {
    for (const child of node.data as GridNode[]) {
      const active = activeViewInGroup(child, groupId);
      if (active) return active;
    }
    return undefined;
  }
  const leaf = node.data as unknown as { id?: string; activeView?: string };
  return groupId === undefined || leaf.id === groupId ? leaf.activeView : undefined;
}

// Panel ids also live in the grid tree (each leaf group's `views` + optional
// `activeView`), independent of the flat `panels` map — a rename or drop in
// `panels` must be mirrored there or dockview restores a group referencing a
// panel id that no longer exists.
//
// A drop can also empty out a leaf entirely (the retired panel was the only
// tab in its group) — an empty `views` array restores as a blank pane, so
// such leaves are pruned (return null) rather than kept with zero views.
// Pruning propagates upward: a branch left with no surviving children is
// itself pruned, and a branch left with exactly one surviving child
// collapses to that child (dockview's SerializedGridObject shape has no
// concept of a branch with a single member — the branch node's own
// `size`/`visible` describe the slot it held in ITS parent, so those are
// preserved on the promoted child; the child's own size was relative to the
// discarded branch and no longer applies now that it fills the whole slot).
function migrateGridNode(
  node: GridNode,
  renamed: Readonly<Record<string, string>>,
  dropped: ReadonlySet<string>,
): GridNode | null {
  if (node.type === "branch") {
    const children = (node.data as GridNode[])
      .map((child) => migrateGridNode(child, renamed, dropped))
      .filter((child): child is GridNode => child !== null);
    if (children.length === 0) return null;
    if (children.length === 1) {
      const [only] = children;
      return { ...only, size: node.size, visible: node.visible } as GridNode;
    }
    return { ...node, data: children } as GridNode;
  }
  const leaf = node.data as unknown as {
    views: string[];
    activeView?: string;
    [key: string]: unknown;
  };
  const views = leaf.views.filter((v) => !dropped.has(v)).map((v) => renamed[v] ?? v);
  if (views.length === 0) return null;
  let activeView = leaf.activeView;
  if (activeView !== undefined) {
    activeView = dropped.has(activeView) ? views[0] : (renamed[activeView] ?? activeView);
  }
  return { ...node, data: { ...leaf, views, activeView } } as GridNode;
}

// Collect every surviving group id (leaf `data.id`) in a pruned grid tree, so
// `activeGroup` can be repointed off a group that pruning just removed.
function collectGroupIds(node: GridNode, out: Set<string>): void {
  if (node.type === "branch") {
    for (const child of node.data as GridNode[]) collectGroupIds(child, out);
    return;
  }
  const leaf = node.data as unknown as { id?: string };
  if (leaf.id) out.add(leaf.id);
}

export function layoutStorageKey(scopeId: string): string {
  return `${KEY_PREFIX}:${scopeId}`;
}

// Persisted blobs carry the tab title dockview serialized at save time, and
// fromJSON restores it verbatim — a registry rename would otherwise never
// reach a pre-existing layout. Re-derive the title from panelRegistry via the
// panel's params (post-migration, so a renamed panel resolves by its new
// key); null = "can't resolve this shape", caller keeps the stored title.
function canonicalPanelTitle(
  component: string,
  params: Record<string, unknown> | undefined,
): string | null {
  let key: string;
  const p: Record<string, string> = {};
  switch (component) {
    case "chat": {
      key = "chat";
      p.agent = typeof params?.agentId === "string" ? params.agentId : DEFAULT_AGENT_SENTINEL;
      break;
    }
    case "teams": {
      if (typeof params?.teamsKey !== "string") return null;
      key = `teams-${params.teamsKey}`;
      if (typeof params.detailId === "string") p.detailId = params.detailId;
      break;
    }
    case "control": {
      // Covers both CONTROL_KEYS panels and the agent-inspector, whose
      // controlKey doubles as its registry key.
      if (typeof params?.controlKey !== "string") return null;
      key = params.controlKey;
      break;
    }
    case "file-viewer":
    case "browser":
      key = component;
      break;
    default:
      return null;
  }
  return describePanel(key, p)?.title ?? null;
}

// fromJSON THROWS (after clearing the dock) on an unregistered
// contentComponent — one stale key must not nuke the whole saved layout.
// Component names live only in the flat panels map (the grid tree references
// panel IDs), so remapping unknowns to the registered 'missing' placeholder
// is sufficient — no grid surgery needed. Also register 'unknown' in the
// components map: the deserializer defaults a missing contentComponent to
// the literal string 'unknown'.
//
// A settled, non-empty roster can identify a stale agent target; reset that
// target to the default while keeping Messages and all valid auxiliary panels.
// An omitted or empty roster is unknown, so preserve the requested agent.
/**
 * Panel ids whose SURFACE was deleted (not renamed, not absorbed): the Brain
 * memory wiki and the dreams/sessions/channels control panels. A saved layout
 * holding one must lose it on restore — see the drop in `sanitizeLayout`.
 * Distinct from `RETIRED_PANEL_ALIASES`, which renames a key whose surface
 * still exists.
 *
 * `browser` is NOT here: #2779 dropped it with the container VNC panel, and the
 * AgentCore live view took the same id back. Leaving it listed would silently
 * delete the new panel from every restored layout.
 */
const RETIRED_PANEL_IDS: ReadonlySet<string> = new Set([
  "teams-brain",
  "control:dreams",
  "control:sessions",
  "control:channels",
]);

export function sanitizeLayout(
  raw: SerializedDockview,
  knownAgentIds?: readonly string[],
): SerializedDockview {
  const knownAgents = knownAgentIds?.length ? new Set(knownAgentIds) : null;
  const rawPanels = raw.panels ?? {};
  const panels: SerializedDockview["panels"] = {};
  const renamed: Record<string, string> = {};
  const dropped = new Set<string>();
  const chatIds = Object.keys(rawPanels).filter((id) => rawPanels[id].contentComponent === "chat");
  const activeView = activeViewInGroup(raw.grid.root, raw.activeGroup);
  const chatWinner = activeView && chatIds.includes(activeView)
    ? activeView
    : chatIds.includes(MESSAGES_PANEL_ID) ? MESSAGES_PANEL_ID : chatIds[0];
  // Targets already produced by this migration pass (by an original panel
  // id or by an earlier retired-panel rename). Two retired panels can alias
  // to the same target (e.g. teams-skills and teams-costs both -> the same
  // consolidated panel) — without this, the second rename would overwrite
  // the first in `panels`, leaving a duplicate view id in the grid tree.
  const claimedTargets = new Set<string>();

  for (const [id, state] of Object.entries(rawPanels)) {
    // Docked session panels were retired in favor of a middle-pane focus
    // placement (SessionFocus). Unlike an ordinary unrecognized
    // contentComponent, this is a KNOWN retired shape: remapping it to
    // 'missing' would just leave a dead tab behind after every upgrade, so it
    // is dropped the same way a retired-panel duplicate is dropped, pruning
    // its now-empty leaf along with it.
    if (id.startsWith("session:")) {
      dropped.add(id);
      continue;
    }
    // Same shape, same reason: panels whose surface was deleted outright with
    // the OpenClaw runtime. Without this they would restore as an "Unknown
    // panel" (teams-brain) or silently render Overview through
    // ControlPanelRouter's fallback (the three control keys).
    if (RETIRED_PANEL_IDS.has(id)) {
      dropped.add(id);
      continue;
    }
    const component =
      state.contentComponent && PANEL_COMPONENT_KEYS.has(state.contentComponent)
        ? state.contentComponent
        : "missing";
    if (component === "chat" && id !== chatWinner) {
      dropped.add(id);
      continue;
    }

    let params = state.params;
    if (params) {
      params = { ...params };
      for (const oneShot of ONE_SHOT_PARAMS) delete params[oneShot];
      for (const key of PERSIST_ONLY_STRIP) delete params[key];
    }

    let finalId = id;
    if (component === "chat") {
      finalId = MESSAGES_PANEL_ID;
      if (id !== finalId) renamed[id] = finalId;
      // Legacy IDs can recover an omitted agent param. A parameterless live
      // Messages panel means a channel/human/group room, so keep it unbound.
      let agentId = typeof params?.agentId === "string" ? params.agentId
        : id !== MESSAGES_PANEL_ID && id.startsWith("chat:") ? id.slice("chat:".length) : undefined;
      if (agentId && knownAgents && agentId !== DEFAULT_AGENT_SENTINEL && !knownAgents.has(agentId)) {
        agentId = DEFAULT_AGENT_SENTINEL;
      }
      params = agentId ? { agentId } : {};
    }
    const migration = migrateRetiredPanelId(id);
    if (migration) {
      if (Object.hasOwn(rawPanels, migration.id) || claimedTargets.has(migration.id)) {
        // Target panel is already open in this same layout, or an earlier
        // retired panel in this same pass already renamed to it — dropping
        // the retired duplicate is simpler and safer than merging two panel
        // states into one id.
        dropped.add(id);
        continue;
      }
      finalId = migration.id;
      renamed[id] = finalId;
      claimedTargets.add(finalId);
      if (params) {
        if (migration.teamsKey !== undefined) params.teamsKey = migration.teamsKey;
        if (migration.controlKey !== undefined) params.controlKey = migration.controlKey;
        // Crossing control -> teams drops the stale controlKey — a teams panel
        // takes only teamsKey, and a leftover controlKey would just be dead
        // weight in the persisted params.
        if (migration.component === "teams") delete params.controlKey;
      }
    }

    // A retired alias can cross namespaces (control:x -> teams-y), so
    // the panel's real component is the migrated one — and the healed title has
    // to be read off THAT, or a migrated panel keeps the retired panel's name.
    const effectiveComponent = migration?.component ?? component;
    // Heal a stale serialized title (a rename shipped after this blob was
    // saved); never invent one where none was stored.
    const healedTitle =
      typeof state.title === "string" ? (canonicalPanelTitle(effectiveComponent, params) ?? state.title) : null;
    panels[finalId] = {
      ...state,
      id: finalId,
      contentComponent: effectiveComponent,
      params,
      ...(healedTitle !== null ? { title: healedTitle } : {}),
    };
  }

  let grid = raw.grid;
  let activeGroup = raw.activeGroup;
  if (Object.keys(renamed).length || dropped.size) {
    // The root prunes away only if literally every panel got dropped —
    // impossible from a duplicate-target drop (the alias target survives
    // elsewhere). Keep the original root rather than crash on invalid data;
    // readLayout turns the resulting empty-panels layout into a null read, so
    // the shell builds Mission Control instead of restoring a grid whose views
    // no longer exist.
    const migratedRoot = migrateGridNode(raw.grid.root, renamed, dropped);
    if (migratedRoot) {
      grid = { ...raw.grid, root: migratedRoot };
      if (activeGroup !== undefined) {
        const survivingGroupIds = new Set<string>();
        collectGroupIds(migratedRoot, survivingGroupIds);
        if (!survivingGroupIds.has(activeGroup)) {
          activeGroup = survivingGroupIds.values().next().value;
        }
      }
    }
  }
  const clean: SerializedDockview = { grid, panels };
  if (activeGroup !== undefined) clean.activeGroup = activeGroup;
  // floatingGroups / popoutGroups / edgeGroups deliberately dropped:
  // disableFloatingGroups only blocks user-initiated floats — persisted
  // floating groups would still restore (and popouts window.open a 404).
  return clean;
}

export function readLayout(
  scopeId: string,
  knownAgentIds?: readonly string[],
): SerializedDockview | null {
  if (typeof window === "undefined") return null;
  try {
    for (const legacy of LEGACY_KEY_PREFIXES) {
      window.localStorage.removeItem(`${legacy}:${scopeId}`);
    }
    const raw = window.localStorage.getItem(layoutStorageKey(scopeId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredLayout>;
    if (parsed.version !== 3 || !parsed.layout || typeof parsed.layout !== "object") return null;
    const clean = sanitizeLayout(parsed.layout, knownAgentIds);
    // Nothing survived sanitizing — treat as "no saved layout" so the caller
    // builds the default rather than restoring a grid with no panels in it.
    return Object.keys(clean.panels ?? {}).length ? clean : null;
  } catch {
    return null;
  }
}

export function writeLayout(scopeId: string, layout: SerializedDockview): void {
  if (typeof window === "undefined") return;
  try {
    const stored: StoredLayout = {
      version: 3,
      savedAt: new Date().toISOString(),
      layout: sanitizeLayout(layout),
    };
    window.localStorage.setItem(layoutStorageKey(scopeId), JSON.stringify(stored));
  } catch {
    // quota / privacy mode — non-persistent session, never crash
  }
}

export function clearLayout(scopeId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(layoutStorageKey(scopeId));
  } catch {
    // best effort
  }
}

// Where the rail opens Tasks: "split" beside the conversation (default) or a
// full "tab" in the Messages group. Per viewer, not per workspace — it is a
// reading preference. Same null-safe read / swallow-on-failure write posture
// as the layout blob: a blocked store just means the default every time.
export type TasksPlacement = "split" | "tab";
export const TASKS_PLACEMENT_KEY = "lightbulb.workspace.tasksPlacement.v1";

export function readTasksPlacement(): TasksPlacement {
  if (typeof window === "undefined") return "split";
  try {
    return window.localStorage.getItem(TASKS_PLACEMENT_KEY) === "tab" ? "tab" : "split";
  } catch {
    return "split";
  }
}

export function writeTasksPlacement(placement: TasksPlacement): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(TASKS_PLACEMENT_KEY, placement);
  } catch {
    // quota / privacy mode — the choice just isn't remembered
  }
}
