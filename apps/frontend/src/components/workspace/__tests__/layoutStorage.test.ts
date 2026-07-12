import { describe, it, expect, beforeEach, afterAll } from "vitest";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";
import type { SerializedDockview } from "dockview-react";
import {
  layoutStorageKey,
  readLayout,
  writeLayout,
  clearLayout,
  sanitizeLayout,
} from "../layoutStorage";

const restore = installStoragePolyfill();
afterAll(restore);
beforeEach(() => window.localStorage.clear());

function minimalLayout(panels: SerializedDockview["panels"]): SerializedDockview {
  return {
    grid: {
      root: { type: "branch", data: [] },
      height: 600,
      width: 800,
      orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
    },
    panels,
  };
}

describe("layoutStorage", () => {
  it("scopes the key per owner", () => {
    expect(layoutStorageKey("org_1")).toBe("isol8.workspace.layout.v1:org_1");
  });

  it("round-trips a valid layout", () => {
    const layout = minimalLayout({
      "teams-inbox": { id: "teams-inbox", contentComponent: "teams", params: { teamsKey: "inbox" } },
    });
    writeLayout("u1", layout);
    expect(readLayout("u1")).toEqual(layout);
  });

  it("returns null on version mismatch or garbage", () => {
    window.localStorage.setItem(layoutStorageKey("u1"), JSON.stringify({ version: 2, layout: {} }));
    expect(readLayout("u1")).toBeNull();
    window.localStorage.setItem(layoutStorageKey("u1"), "not-json{");
    expect(readLayout("u1")).toBeNull();
  });

  it("sanitizer remaps unknown contentComponent to 'missing' (fromJSON throws otherwise)", () => {
    const dirty = minimalLayout({
      ghost: { id: "ghost", contentComponent: "deleted-panel-type", params: {} },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels.ghost.contentComponent).toBe("missing");
  });

  it("sanitizer strips floating/popout/edge groups and one-shot params", () => {
    const dirty = {
      ...minimalLayout({
        "teams-issues": {
          id: "teams-issues",
          contentComponent: "teams",
          params: { teamsKey: "issues", new: "task", agent: "a-1" },
        },
      }),
      floatingGroups: [{}],
      popoutGroups: [{}],
      edgeGroups: {},
    } as unknown as SerializedDockview;
    const clean = sanitizeLayout(dirty);
    expect(clean.floatingGroups).toBeUndefined();
    expect(clean.popoutGroups).toBeUndefined();
    expect((clean as unknown as Record<string, unknown>).edgeGroups).toBeUndefined();
    expect(clean.panels["teams-issues"].params).toEqual({ teamsKey: "issues", agent: "a-1" });
  });

  it("sanitizer strips persist-only params (initialTab) so a nav intent can't restore forever", () => {
    const dirty = minimalLayout({
      "agent-inspector": {
        id: "agent-inspector",
        contentComponent: "control",
        params: { controlKey: "agent-inspector", agentId: "a", initialTab: "channels" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["agent-inspector"].params).toEqual({
      controlKey: "agent-inspector",
      agentId: "a",
    });
  });

  it("clearLayout removes only its own key", () => {
    writeLayout("u1", minimalLayout({}));
    window.localStorage.setItem("unrelated", "x");
    clearLayout("u1");
    expect(readLayout("u1")).toBeNull();
    expect(window.localStorage.getItem("unrelated")).toBe("x");
  });
});
