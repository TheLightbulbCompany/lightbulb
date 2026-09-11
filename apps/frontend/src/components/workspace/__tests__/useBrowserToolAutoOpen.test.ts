import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";
import type { ChatIncomingMessage } from "@lightbulb/shared";

const openPanel = vi.fn();
const getWorkspaceSnapshot = vi.fn(() => ({ ready: true, openPanelIds: [] as string[] }));
vi.mock("../workspaceController", () => ({
  openPanel: (...args: unknown[]) => openPanel(...args),
  getWorkspaceSnapshot: () => getWorkspaceSnapshot(),
}));

let chatHandler: ((msg: ChatIncomingMessage) => void) | null = null;
const unsubscribe = vi.fn();
vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    onChatMessage: (handler: (msg: ChatIncomingMessage) => void) => {
      chatHandler = handler;
      return unsubscribe;
    },
  }),
}));

import { shouldSurfaceBrowser, useBrowserToolAutoOpen } from "../useBrowserToolAutoOpen";

const toolStart = (tool: string, runId = "run-1"): ChatIncomingMessage => ({ type: "tool_start", tool, runId });
const runEnded = (runId = "run-1"): ChatIncomingMessage => ({ type: "run_ended", runId, outcome: "done" });
const DOCK = { ready: true, openPanelIds: [] as string[] };

describe("shouldSurfaceBrowser", () => {
  it("surfaces on the native browser tool, once per run", () => {
    const seen = new Set<string>();
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, DOCK)).toBe(true);
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, DOCK)).toBe(false);
  });

  it("surfaces on the CLI-harness spelling too", () => {
    expect(shouldSurfaceBrowser(toolStart("mcp__openclaw__browser"), new Set(), DOCK)).toBe(true);
  });

  it("ignores other tools and other frame types", () => {
    const seen = new Set<string>();
    expect(shouldSurfaceBrowser(toolStart("exec"), seen, DOCK)).toBe(false);
    expect(shouldSurfaceBrowser({ type: "tool_end", tool: "browser", runId: "run-1" }, seen, DOCK)).toBe(false);
    expect(seen.size).toBe(0);
  });

  it("never steals focus when the panel is already in the layout, and still counts the run as seen", () => {
    const seen = new Set<string>();
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, { ready: true, openPanelIds: ["browser"] })).toBe(false);
    // Owner closes the panel mid-run; the same run must not re-open it.
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, DOCK)).toBe(false);
  });

  it("waits for the dock: before attach it neither opens nor marks the run seen", () => {
    const seen = new Set<string>();
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, { ready: false, openPanelIds: [] })).toBe(false);
    expect(seen.size).toBe(0);
    // Next browser call in the same run, dock now attached → opens.
    expect(shouldSurfaceBrowser(toolStart("browser"), seen, DOCK)).toBe(true);
  });

  it("forgets a run on run_ended so memory does not grow", () => {
    const seen = new Set<string>();
    shouldSurfaceBrowser(toolStart("browser"), seen, DOCK);
    expect(seen.has("run-1")).toBe(true);
    expect(shouldSurfaceBrowser(runEnded(), seen, DOCK)).toBe(false);
    expect(seen.has("run-1")).toBe(false);
  });
});

describe("useBrowserToolAutoOpen", () => {
  beforeEach(() => {
    openPanel.mockClear();
    unsubscribe.mockClear();
    getWorkspaceSnapshot.mockReturnValue({ ready: true, openPanelIds: [] });
    chatHandler = null;
  });

  it("opens the browser panel on the first browser tool_start of a run", () => {
    renderHook(() => useBrowserToolAutoOpen());
    expect(chatHandler).not.toBeNull();
    chatHandler!(toolStart("mcp__openclaw__browser"));
    chatHandler!(toolStart("browser"));
    expect(openPanel).toHaveBeenCalledTimes(1);
    expect(openPanel).toHaveBeenCalledWith("browser");
  });

  it("reads the live layout — no open when the panel already exists", () => {
    getWorkspaceSnapshot.mockReturnValue({ ready: true, openPanelIds: ["browser"] });
    renderHook(() => useBrowserToolAutoOpen());
    chatHandler!(toolStart("browser"));
    expect(openPanel).not.toHaveBeenCalled();
  });

  it("unsubscribes on unmount", () => {
    const { unmount } = renderHook(() => useBrowserToolAutoOpen());
    unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
  });
});
