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
} from "./panelRegistry";

// v2: Mission Control put Browser (not Dashboard) in the right-top slot. The
// bump orphans every v1 blob so all owners land on the new default once;
// readLayout drops the dead v1 key so it does not sit in localStorage forever.
const KEY_PREFIX = "isol8.workspace.layout.v2";
const LEGACY_KEY_PREFIX = "isol8.workspace.layout.v1";

// Params that may live on a panel while it's open but must NOT survive into
// the persisted blob. initialTab is a navigation intent (the Get Started
// "Connect a channel" CTA), not view state — persisting it would land the
// inspector on that tab on EVERY restore, forever. Same for the inbox's
// deep-linked tab: the user's chosen tab lives in localStorage, and a
// persisted tab param would silently override it on every reload.
const PERSIST_ONLY_STRIP = ["initialTab", "tab"] as const;

interface StoredLayout {
  version: 2;
  savedAt: string;
  layout: SerializedDockview;
}

function hasOwnKey(obj: object, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(obj, key);
}

// RETIRED_PANEL_ALIASES is otherwise applied replace-only to URL commands
// (see panelRegistry.ts) — a saved dockview layout restores panel ids/params
// straight from localStorage, bypassing that resolver entirely. A layout
// saved before a panel was retired still carries the old id, so it must be
// migrated here too. Two id shapes show up in practice: teams-* aliases are
// bare panel ids (panelId === registry key), non-teams aliases (credits→llm)
// are control-panel ids carrying the `control:` prefix.
function migrateRetiredPanelId(
  id: string,
): { id: string; teamsKey?: string; controlKey?: string } | null {
  if (hasOwnKey(RETIRED_PANEL_ALIASES, id)) {
    const target = RETIRED_PANEL_ALIASES[id];
    if (target.startsWith("teams-")) {
      return { id: target, teamsKey: target.slice("teams-".length) };
    }
    return null;
  }
  if (id.startsWith("control:")) {
    const key = id.slice("control:".length);
    if (hasOwnKey(RETIRED_PANEL_ALIASES, key)) {
      const target = RETIRED_PANEL_ALIASES[key];
      return { id: `control:${target}`, controlKey: target };
    }
  }
  return null;
}

type GridNode = SerializedDockview["grid"]["root"];

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

// fromJSON THROWS (after clearing the dock) on an unregistered
// contentComponent — one stale key must not nuke the whole saved layout.
// Component names live only in the flat panels map (the grid tree references
// panel IDs), so remapping unknowns to the registered 'missing' placeholder
// is sufficient — no grid surgery needed. Also register 'unknown' in the
// components map: the deserializer defaults a missing contentComponent to
// the literal string 'unknown'.
//
// `knownAgentIds` (optional) prunes chat panels for agents that no longer
// exist — a reprovision/wipe leaves the layout pointing at dead ids, which
// restore as a tab the user has to close by hand (ChatPanel's "This agent no
// longer exists."). PASS IT ONLY FROM A CALLER HOLDING A SETTLED ROSTER: an
// empty or omitted list means "roster unknown", never "the user has no
// agents", because pruning against an in-flight roster would delete every
// chat panel on every restore. The non-empty check is the guard — a settled
// roster that is genuinely empty is indistinguishable from a loading one here
// and is treated as unknown (worst case the dead panel survives one more
// load, which is exactly today's behaviour).
export function sanitizeLayout(
  raw: SerializedDockview,
  knownAgentIds?: readonly string[],
): SerializedDockview {
  const knownAgents = knownAgentIds?.length ? new Set(knownAgentIds) : null;
  const rawPanels = raw.panels ?? {};
  const panels: SerializedDockview["panels"] = {};
  const renamed: Record<string, string> = {};
  const dropped = new Set<string>();
  // Targets already produced by this migration pass (by an original panel
  // id or by an earlier retired-panel rename). Two retired panels can alias
  // to the same target (e.g. teams-skills and teams-costs both -> the same
  // consolidated panel) — without this, the second rename would overwrite
  // the first in `panels`, leaving a duplicate view id in the grid tree.
  const claimedTargets = new Set<string>();

  for (const [id, state] of Object.entries(rawPanels)) {
    const component =
      state.contentComponent && PANEL_COMPONENT_KEYS.has(state.contentComponent)
        ? state.contentComponent
        : "missing";
    // Dead-agent chat panel: drop it (the existing `dropped` machinery mirrors
    // the removal into the grid tree and collapses whatever it empties). The
    // sentinel id resolves at render time, so it is never stale.
    if (component === "chat" && knownAgents) {
      const agentId = (state.params as { agentId?: unknown } | undefined)?.agentId;
      if (
        typeof agentId === "string"
        && agentId !== DEFAULT_AGENT_SENTINEL
        && !knownAgents.has(agentId)
      ) {
        dropped.add(id);
        continue;
      }
    }

    let params = state.params;
    if (params) {
      params = { ...params };
      for (const oneShot of ONE_SHOT_PARAMS) delete params[oneShot];
      for (const key of PERSIST_ONLY_STRIP) delete params[key];
    }

    let finalId = id;
    const migration = migrateRetiredPanelId(id);
    if (migration) {
      if (hasOwnKey(rawPanels, migration.id) || claimedTargets.has(migration.id)) {
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
      }
    }

    panels[finalId] = { ...state, id: finalId, contentComponent: component, params };
  }

  let grid = raw.grid;
  let activeGroup = raw.activeGroup;
  if (Object.keys(renamed).length || dropped.size) {
    // The root prunes away only if literally every panel got dropped —
    // impossible from a duplicate-target drop (the alias target survives
    // elsewhere), but reachable from agent pruning (a layout that is nothing
    // but chats for deleted agents). Keep the original root rather than crash;
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
    window.localStorage.removeItem(`${LEGACY_KEY_PREFIX}:${scopeId}`);
    const raw = window.localStorage.getItem(layoutStorageKey(scopeId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredLayout>;
    if (parsed.version !== 2 || !parsed.layout || typeof parsed.layout !== "object") return null;
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
      version: 2,
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
