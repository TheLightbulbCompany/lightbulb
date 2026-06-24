// Ported from upstream Paperclip's OrgChart layout algorithm
// (paperclip/ui/src/pages/OrgChart.tsx) (MIT, (c) 2025 Paperclip AI).
// Pure subtreeWidth-based tree positioning. Pan/zoom lives in
// `useOrgPanZoom` (the viewport transform); the layout stays gesture-free
// so it remains a pure function of the agent list.

import type { CompanyAgent } from "@/components/teams/shared/types";

// Layout constants — match upstream geometry. `CARD_H` is taller than
// upstream's 100 because our editorial card carries the enriched body
// (title, capabilities, last-active) rather than a single role line.
export const CARD_W = 200;
export const CARD_H = 132;
export const GAP_X = 32;
export const GAP_Y = 80;
export const PADDING = 60;

// Zoom bounds + helper, shared by the layout module and the pan/zoom hook
// so the viewport and any tests agree on the clamp. Mirrors upstream's
// MIN_ZOOM/MAX_ZOOM (paperclip/ui/src/pages/OrgChart.tsx).
export const MIN_ZOOM = 0.2;
export const MAX_ZOOM = 2;

/** Clamp a zoom factor into the [MIN_ZOOM, MAX_ZOOM] range. */
export function clampZoom(value: number): number {
  return Math.min(Math.max(value, MIN_ZOOM), MAX_ZOOM);
}

export interface OrgChartAgent extends CompanyAgent {
  /** ID of the agent this one reports to (null = root). */
  reportsTo?: string | null;
  /** Status string from BFF: "idle" | "running" | "error" | "terminated" | etc. */
  status?: string | null;
  /** Display role e.g. "ceo" | "engineer" — optional, falls back to "Agent". */
  role?: string | null;
  // ── Enrichment fields the BFF proxies from the upstream Agent record.
  // `title`, `capabilities` are already declared on CompanyAgent; these three
  // are extra wire fields the org chart consumes. ──
  /** ISO timestamp of the agent's last heartbeat. */
  lastHeartbeatAt?: string | null;
  /** Reason the agent is paused (only meaningful when status is "paused"). */
  pauseReason?: string | null;
  /** Failure detail (only meaningful when status is "error"). */
  errorReason?: string | null;
}

export interface LayoutNode {
  id: string;
  name: string;
  role: string;
  status: string;
  x: number;
  y: number;
  children: LayoutNode[];
  // ── Enrichment carried through to AgentCard ──
  // All optional: the chart degrades to name+role+status when a field is
  // absent on the wire. Sourced from the upstream Paperclip Agent record
  // the BFF proxies on `GET /teams/agents`.
  /** Short one-liner shown under the name (upstream `Agent.title`). */
  title?: string | null;
  /** Longer freeform description (upstream `Agent.capabilities`). */
  capabilities?: string | null;
  /** ISO timestamp of the agent's last heartbeat (upstream `Agent.lastHeartbeatAt`). */
  lastHeartbeatAt?: string | null;
  /** Why the agent is paused, when status is "paused" (upstream `Agent.pauseReason`). */
  pauseReason?: string | null;
  /** Failure detail, when status is "error" (upstream `Agent.errorReason`). */
  errorReason?: string | null;
}

/**
 * Compute the rendering width a subtree needs (in px). A leaf is just the card
 * width; a parent is the larger of its own card width or the summed widths of
 * its children plus gaps. Memoized via the per-id `cache` map to avoid O(n^2)
 * recomputation.
 */
export function subtreeWidth(node: LayoutNode, cache: Map<string, number>): number {
  const cached = cache.get(node.id);
  if (cached !== undefined) return cached;
  let result: number;
  if (node.children.length === 0) {
    result = CARD_W;
  } else {
    let sum = 0;
    for (const child of node.children) sum += subtreeWidth(child, cache);
    sum += (node.children.length - 1) * GAP_X;
    result = Math.max(CARD_W, sum);
  }
  cache.set(node.id, result);
  return result;
}

/**
 * Position each node in a top-down tree. Children are centered under their
 * parent. Roots laid out horizontally side-by-side.
 *
 * Cycle handling: agents whose reportsTo chain forms a cycle are demoted to
 * roots (their reportsTo is treated as null). A parent referencing a missing
 * id is also treated as a root.
 */
export function layoutTree(agents: OrgChartAgent[]): {
  roots: LayoutNode[];
  width: number;
  height: number;
} {
  if (agents.length === 0) return { roots: [], width: 0, height: 0 };

  const byId = new Map<string, OrgChartAgent>();
  for (const a of agents) byId.set(a.id, a);

  // Resolve the effective parent of an agent:
  //   - null         => root
  //   - string id    => that parent (only when the chain to a root is acyclic
  //                     and every link resolves to a known agent)
  // If we hit a cycle or a dangling reference anywhere up the chain, the
  // original agent is demoted to a root.
  function effectiveParent(start: OrgChartAgent): string | null {
    if (!start.reportsTo) return null;
    let current: string | null = start.reportsTo;
    const seen = new Set<string>([start.id]);
    while (current) {
      if (seen.has(current)) return null; // cycle -> root
      seen.add(current);
      const parent = byId.get(current);
      if (!parent) return null; // dangling reference -> root
      current = parent.reportsTo ?? null;
    }
    return start.reportsTo;
  }

  // Build child relationships: parent_id (or null) -> ordered list of child ids.
  const childrenOf = new Map<string | null, string[]>();
  for (const a of agents) {
    const parent = effectiveParent(a);
    if (!childrenOf.has(parent)) childrenOf.set(parent, []);
    childrenOf.get(parent)!.push(a.id);
  }

  function buildNode(id: string): LayoutNode {
    const a = byId.get(id)!;
    const childIds = childrenOf.get(id) ?? [];
    const children = childIds.map(buildNode);
    return {
      id: a.id,
      name: a.name,
      role: a.role ?? "Agent",
      status: a.status ?? "idle",
      x: 0,
      y: 0,
      children,
      title: a.title ?? null,
      capabilities: a.capabilities ?? null,
      lastHeartbeatAt: a.lastHeartbeatAt ?? null,
      pauseReason: a.pauseReason ?? null,
      errorReason: a.errorReason ?? null,
    };
  }

  const rootIds = childrenOf.get(null) ?? [];
  const widths = new Map<string, number>();
  const rootsRaw = rootIds.map(buildNode);

  // Position pass: walk each root, recursively position children.
  function positionNode(node: LayoutNode, x: number, y: number) {
    node.x = x;
    node.y = y;
    if (node.children.length === 0) return;
    const childWidths = node.children.map((c) => subtreeWidth(c, widths));
    const totalChildrenW =
      childWidths.reduce((a, b) => a + b, 0) +
      (node.children.length - 1) * GAP_X;
    // Children row's left edge so the row is centered under the parent's card.
    let childX = x + (CARD_W - totalChildrenW) / 2;
    for (let i = 0; i < node.children.length; i++) {
      const cw = childWidths[i];
      const child = node.children[i];
      // Child card's x within its allocated subtree-width slot.
      const childCenterX = childX + cw / 2 - CARD_W / 2;
      positionNode(child, childCenterX, y + CARD_H + GAP_Y);
      childX += cw + GAP_X;
    }
  }

  // Lay out roots side-by-side.
  let cursorX = PADDING;
  for (const root of rootsRaw) {
    const w = subtreeWidth(root, widths);
    positionNode(root, cursorX + (w - CARD_W) / 2, PADDING);
    cursorX += w + GAP_X * 2; // extra gap between independent trees
  }

  // Compute total bounding box.
  let maxX = 0;
  let maxY = 0;
  function visit(node: LayoutNode) {
    maxX = Math.max(maxX, node.x + CARD_W);
    maxY = Math.max(maxY, node.y + CARD_H);
    for (const c of node.children) visit(c);
  }
  for (const r of rootsRaw) visit(r);

  return {
    roots: rootsRaw,
    width: maxX + PADDING,
    height: maxY + PADDING,
  };
}

/** Flatten a tree into a list (useful for rendering all cards). */
export function flattenTree(roots: LayoutNode[]): LayoutNode[] {
  const out: LayoutNode[] = [];
  function visit(n: LayoutNode) {
    out.push(n);
    for (const c of n.children) visit(c);
  }
  for (const r of roots) visit(r);
  return out;
}

/** Yield (parent, child) pairs for SVG edge rendering. */
export function flattenEdges(
  roots: LayoutNode[],
): { from: LayoutNode; to: LayoutNode }[] {
  const out: { from: LayoutNode; to: LayoutNode }[] = [];
  function visit(n: LayoutNode) {
    for (const c of n.children) {
      out.push({ from: n, to: c });
      visit(c);
    }
  }
  for (const r of roots) visit(r);
  return out;
}
