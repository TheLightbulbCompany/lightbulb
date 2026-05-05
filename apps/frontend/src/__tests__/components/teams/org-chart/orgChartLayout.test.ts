import { test, expect } from "vitest";
import {
  CARD_W,
  CARD_H,
  GAP_X,
  GAP_Y,
  PADDING,
  layoutTree,
  flattenTree,
  flattenEdges,
  type OrgChartAgent,
  type LayoutNode,
} from "@/components/teams/org-chart/orgChartLayout";

test("layoutTree on empty agents returns empty roots and zero dimensions", () => {
  const out = layoutTree([]);
  expect(out.roots).toEqual([]);
  expect(out.width).toBe(0);
  expect(out.height).toBe(0);
});

test("layoutTree on a single root agent returns one root with no children", () => {
  const agents: OrgChartAgent[] = [
    { id: "a", name: "Alpha", reportsTo: null, role: "ceo", status: "idle" },
  ];
  const { roots, width, height } = layoutTree(agents);
  expect(roots).toHaveLength(1);
  expect(roots[0].id).toBe("a");
  expect(roots[0].children).toEqual([]);
  expect(roots[0].x).toBe(PADDING);
  expect(roots[0].y).toBe(PADDING);
  expect(width).toBe(PADDING + CARD_W + PADDING);
  expect(height).toBe(PADDING + CARD_H + PADDING);
});

test("layoutTree positions a single child below its parent (same x)", () => {
  const agents: OrgChartAgent[] = [
    { id: "p", name: "Parent", reportsTo: null },
    { id: "c", name: "Child", reportsTo: "p" },
  ];
  const { roots } = layoutTree(agents);
  const parent = roots[0];
  expect(parent.children).toHaveLength(1);
  const child = parent.children[0];
  // Single child has subtree width == CARD_W, so it sits directly under parent.
  expect(child.x).toBe(parent.x);
  expect(child.y).toBe(parent.y + CARD_H + GAP_Y);
});

test("layoutTree centers two children side-by-side under the parent", () => {
  const agents: OrgChartAgent[] = [
    { id: "p", name: "Parent", reportsTo: null },
    { id: "c1", name: "Child 1", reportsTo: "p" },
    { id: "c2", name: "Child 2", reportsTo: "p" },
  ];
  const { roots } = layoutTree(agents);
  const parent = roots[0];
  expect(parent.children).toHaveLength(2);
  const [c1, c2] = parent.children;

  // Children are side-by-side at the same y.
  expect(c1.y).toBe(c2.y);
  expect(c2.y).toBe(parent.y + CARD_H + GAP_Y);
  expect(c2.x).toBe(c1.x + CARD_W + GAP_X);

  // Parent is x-centered over the children row.
  const childrenCenterX = (c1.x + c2.x + CARD_W) / 2;
  const parentCenterX = parent.x + CARD_W / 2;
  expect(parentCenterX).toBeCloseTo(childrenCenterX, 5);
});

test("layoutTree handles a 3-level tree grandparent -> parent -> child", () => {
  const agents: OrgChartAgent[] = [
    { id: "g", name: "Grand", reportsTo: null },
    { id: "p", name: "Parent", reportsTo: "g" },
    { id: "c", name: "Child", reportsTo: "p" },
  ];
  const { roots } = layoutTree(agents);
  const grand = roots[0];
  const parent = grand.children[0];
  const child = parent.children[0];

  expect(grand.y).toBe(PADDING);
  expect(parent.y).toBe(grand.y + CARD_H + GAP_Y);
  expect(child.y).toBe(parent.y + CARD_H + GAP_Y);
  // Each level has a single descendant, so x cascades unchanged.
  expect(parent.x).toBe(grand.x);
  expect(child.x).toBe(parent.x);
});

test("layoutTree lays two independent roots out horizontally", () => {
  const agents: OrgChartAgent[] = [
    { id: "r1", name: "Root1", reportsTo: null },
    { id: "r2", name: "Root2", reportsTo: null },
  ];
  const { roots } = layoutTree(agents);
  expect(roots).toHaveLength(2);
  const [r1, r2] = roots;
  expect(r1.y).toBe(r2.y);
  // r2 sits to the right of r1 with at least the inter-tree gap.
  expect(r2.x).toBeGreaterThan(r1.x + CARD_W);
});

test("layoutTree treats a 2-cycle as two independent roots", () => {
  const agents: OrgChartAgent[] = [
    { id: "a", name: "A", reportsTo: "b" },
    { id: "b", name: "B", reportsTo: "a" },
  ];
  const { roots } = layoutTree(agents);
  expect(roots).toHaveLength(2);
  const ids = roots.map((r) => r.id).sort();
  expect(ids).toEqual(["a", "b"]);
  for (const r of roots) expect(r.children).toEqual([]);
});

test("layoutTree treats a dangling reportsTo reference as a root", () => {
  const agents: OrgChartAgent[] = [
    { id: "a", name: "A", reportsTo: "missing-id" },
  ];
  const { roots } = layoutTree(agents);
  expect(roots).toHaveLength(1);
  expect(roots[0].id).toBe("a");
  expect(roots[0].children).toEqual([]);
});

test("flattenTree returns every node in a single tree", () => {
  const agents: OrgChartAgent[] = [
    { id: "g", name: "Grand", reportsTo: null },
    { id: "p", name: "Parent", reportsTo: "g" },
    { id: "c1", name: "Child1", reportsTo: "p" },
    { id: "c2", name: "Child2", reportsTo: "p" },
  ];
  const { roots } = layoutTree(agents);
  const flat = flattenTree(roots);
  const ids = flat.map((n: LayoutNode) => n.id).sort();
  expect(ids).toEqual(["c1", "c2", "g", "p"]);
});

test("flattenEdges returns N-1 edges for a tree of N nodes", () => {
  const agents: OrgChartAgent[] = [
    { id: "g", name: "Grand", reportsTo: null },
    { id: "p", name: "Parent", reportsTo: "g" },
    { id: "c1", name: "Child1", reportsTo: "p" },
    { id: "c2", name: "Child2", reportsTo: "p" },
  ];
  const { roots } = layoutTree(agents);
  const edges = flattenEdges(roots);
  expect(edges).toHaveLength(3);
  const pairs = edges.map((e) => `${e.from.id}->${e.to.id}`).sort();
  expect(pairs).toEqual(["g->p", "p->c1", "p->c2"]);
});
