// @vitest-environment jsdom
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  attachShell,
  openPanel,
  openChatPanel,
  openChatPanelWithDraft,
  openMessagePanel,
  openSessionPanel,
  openBrowserPane,
  closePane,
  closeSubjectDeleted,
  activateMessagesPanel,
  syncMessagesPanelAgent,
  setPaneFullScreen,
  setThreadPaneHost,
  updatePanelParams,
  refreshWorkspaceSnapshot,
  getWorkspaceSnapshot,
  useThreadPane,
  _resetControllerForTests,
} from "../workspaceController";
import { MESSAGES_PANEL_ID } from "../panelRegistry";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import {
  consumeFocusComposerRequest,
  releaseFocusComposerRequest,
  _resetFocusComposerRequestForTests,
} from "@/lib/focusComposerRequest";

const snap = () => getWorkspaceSnapshot();
let defaultAgent: string | undefined;

beforeEach(() => {
  _resetControllerForTests();
  _resetFocusComposerRequestForTests();
  vi.clearAllMocks();
  window.history.replaceState(null, "", "/chat");
  defaultAgent = "agent-ceo";
  attachShell(() => defaultAgent);
});

describe("places", () => {
  it("starts in Chat on the default agent's conversation", () => {
    expect(snap()).toMatchObject({ ready: true, place: "chat", main: null, pane: null, activePanelId: MESSAGES_PANEL_ID });
    expect(snap().chatParams).toEqual({ agentId: "__default__" });
  });

  it.each([
    ["activity", "activity"],
    ["teams-issues", "work"],
    ["teams-routines", "work"],
    ["teams-search", "work"],
    ["teams-artifacts", "library"],
  ])("%s opens the %s place's main view", (key, place) => {
    openPanel(key);
    expect(snap().place).toBe(place);
  });

  it("re-opening a place re-delivers fresh params and clears stale clearable ones", () => {
    openPanel("teams-issues", { agent: "a-1", groupBy: "status" });
    expect(snap().main?.params).toEqual({ teamsKey: "issues", agent: "a-1", groupBy: "status" });
    openPanel("teams-issues", { groupBy: "status" });
    expect(snap().main?.params).toEqual({ teamsKey: "issues", groupBy: "status" });
    openPanel("teams-projects");
    expect(snap().main).toMatchObject({ panelId: "teams-work", params: { teamsKey: "projects" } });
  });

  it("one-shots ride the open and a panel consumes them with updatePanelParams", () => {
    openPanel("teams-issues", {}, { oneShots: { new: "task" } });
    expect(snap().main?.params).toMatchObject({ new: "task" });
    updatePanelParams("teams-work", { new: undefined });
    expect(snap().main?.params).toEqual({ teamsKey: "issues" });
  });

  it("only pushUrl writes the URL, once", () => {
    openPanel("activity");
    expect(pushSearchParams).not.toHaveBeenCalled();
    openPanel("activity", {}, { pushUrl: true });
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    expect(String(vi.mocked(pushSearchParams).mock.calls[0][0])).toBe("panel=activity");
  });

  it("does nothing before the shell is attached", () => {
    _resetControllerForTests();
    openPanel("activity");
    openChatPanel("a-1");
    expect(snap()).toMatchObject({ ready: false, main: null, pane: null });
  });
});

describe("the conversation", () => {
  it("retargets one conversation for every agent, room and session open, and goes to Chat", () => {
    openPanel("teams-issues");
    openChatPanel("agent-a");
    expect(snap()).toMatchObject({ place: "chat", chatParams: { agentId: "agent-a" } });
    openMessagePanel("room-1", "m-1");
    expect(snap().chatParams).toEqual({ roomId: "room-1", messageId: "m-1" });
    openMessagePanel("room-2");
    expect(snap().chatParams).toEqual({ roomId: "room-2" });
    openSessionPanel("s-1");
    const first = snap().chatParams.sessionIntent;
    openSessionPanel("s-1");
    expect(snap().chatParams.sessionId).toBe("s-1");
    expect(snap().chatParams.sessionIntent).not.toBe(first);
    expect(snap().openPanelIds.filter((id) => id === MESSAGES_PANEL_ID)).toHaveLength(1);
  });

  it("activateMessagesPanel shows Chat without an agent target", () => {
    openChatPanel("agent-a");
    openPanel("activity");
    activateMessagesPanel();
    expect(snap()).toMatchObject({ place: "chat", chatParams: { agentSelection: "resolved" } });
    expect(snap().chatParams.agentId).toBeUndefined();
  });

  it("syncs the room's agent as resolved metadata", () => {
    syncMessagesPanelAgent("agent-b");
    expect(snap().chatParams).toEqual({ agentId: "agent-b", agentSelection: "resolved" });
    expect(snap().focusedChatAgentId).toBe("agent-b");
  });

  it("openChatPanelWithDraft defers the draft until the conversation settles; silent with no default", () => {
    expect(openChatPanelWithDraft("hello")).toBe(true);
    expect(consumeFocusComposerRequest("agent-ceo")).toBeNull();
    expect(releaseFocusComposerRequest("agent-ceo")).toBe(true);
    defaultAgent = undefined;
    expect(openChatPanelWithDraft("again")).toBe(false);
  });

  it("resolves the default-agent sentinel when the roster arrives", () => {
    defaultAgent = undefined;
    refreshWorkspaceSnapshot();
    expect(snap().focusedChatAgentId).toBeNull();
    defaultAgent = "agent-late";
    refreshWorkspaceSnapshot();
    expect(snap()).toMatchObject({ focusedChatAgentId: "agent-late", visibleChatAgentIds: ["agent-late"] });
  });

  it("is visible only in Chat or beside a full-screen pane; the last agent stays sticky", () => {
    openChatPanel("agent-a");
    openPanel("teams-issues");
    expect(snap()).toMatchObject({ messagesVisible: false, visibleChatAgentIds: [], focusedChatAgentId: null, lastFocusedChatAgentId: "agent-a" });
    openPanel("file-viewer");
    setPaneFullScreen(true);
    expect(snap()).toMatchObject({ messagesVisible: true, visibleChatAgentIds: ["agent-a"] });
  });

  it("keeps the same snapshot object when nothing changed", () => {
    const before = snap();
    refreshWorkspaceSnapshot();
    expect(snap()).toBe(before);
  });
});

describe("the right pane", () => {
  it.each([
    ["file-viewer", { path: "/a.md" }, "file-viewer"],
    ["browser", {}, "browser"],
    ["mail", { item: "mail:1" }, "mail"],
    ["agents", { agent: "a-1" }, "control:agents"],
    ["teams-issue-detail", { detailId: "ADM-1" }, "teams-issue-detail:ADM-1"],
  ])("%s opens in the pane beside the place", (key, params, paneId) => {
    openPanel("activity");
    openPanel(key, params);
    expect(snap()).toMatchObject({ place: "activity", pane: { panelId: paneId } });
  });

  it("holds one thing at a time: opening another replaces it", () => {
    openPanel("file-viewer", { path: "/a.md" });
    openPanel("file-viewer", { path: "/b.md" });
    expect(snap().pane?.params).toEqual({ path: "/b.md" });
    expect(openBrowserPane()).toBe(true);
    expect(snap().pane?.panelId).toBe("browser");
    expect(snap().openPanelIds).toEqual([MESSAGES_PANEL_ID, "browser"]);
  });

  it("close clears a URL that named it (in place) and nothing else", () => {
    window.history.replaceState(null, "", "/chat?panel=teams-issue-detail&detailId=ADM-1&utm=x");
    openPanel("teams-issue-detail", { detailId: "ADM-1" });
    closePane();
    expect(snap().pane).toBeNull();
    expect(String(vi.mocked(replaceSearchParams).mock.calls[0][0])).toBe("utm=x");
    openPanel("browser");
    closePane();
    expect(replaceSearchParams).toHaveBeenCalledTimes(1);
  });

  it("full screen: needs a pane, folds back when a place is picked or the pane closes", () => {
    setPaneFullScreen(true);
    expect(snap().fullScreen).toBe(false);
    openPanel("browser");
    setPaneFullScreen(true);
    expect(snap()).toMatchObject({ fullScreen: true, messagesVisible: true });
    openPanel("file-viewer"); // another pane keeps the screen
    expect(snap().fullScreen).toBe(true);
    openPanel("teams-issues");
    expect(snap()).toMatchObject({ fullScreen: false, place: "work", pane: { panelId: "file-viewer" } });
    setPaneFullScreen(true);
    closePane();
    expect(snap()).toMatchObject({ fullScreen: false, pane: null });
  });

  it("closeSubjectDeleted replaces the URL with the list, closes the detail, shows the list", () => {
    openPanel("teams-issue-detail", { detailId: "ADM-1" });
    closeSubjectDeleted("teams-issue-detail:ADM-1", "teams-issues");
    expect(String(vi.mocked(replaceSearchParams).mock.calls[0][0])).toBe("panel=teams-issues");
    expect(snap()).toMatchObject({ pane: null, place: "work" });
  });

  it("detach empties both slots", () => {
    _resetControllerForTests();
    const detach = attachShell(() => undefined);
    openPanel("activity");
    openPanel("browser");
    detach();
    expect(snap()).toMatchObject({ ready: false, pane: null, main: null, openPanelIds: [] });
  });
});

describe("useThreadPane (the room's side column)", () => {
  it("claims the pane while open, hands back the host, and folds when something else takes it", () => {
    const host = document.createElement("div");
    const close = vi.fn();
    const { result, rerender } = renderHook(({ open }) => useThreadPane(open, close), { initialProps: { open: false } });
    expect(result.current).toBeNull();
    expect(snap().pane).toBeNull();
    rerender({ open: true });
    expect(snap().pane?.component).toBe("thread");
    act(() => setThreadPaneHost(host));
    expect(result.current).toBe(host);
    act(() => openPanel("browser"));
    expect(close).toHaveBeenCalledTimes(1);
    expect(result.current).toBeNull();
    rerender({ open: false });
    expect(snap().pane?.panelId).toBe("browser");
  });

  it("releases the pane when the column closes, and the pane's close folds the column", () => {
    const close = vi.fn();
    const { rerender } = renderHook(({ open }) => useThreadPane(open, close), { initialProps: { open: true } });
    expect(snap().pane?.component).toBe("thread");
    rerender({ open: false });
    expect(snap().pane).toBeNull();
    rerender({ open: true });
    act(() => closePane());
    expect(close).toHaveBeenCalledTimes(1);
  });

  it("stays inline with no shell mounted", () => {
    _resetControllerForTests();
    const { result } = renderHook(() => useThreadPane(true, vi.fn()));
    expect(result.current).toBeNull();
    expect(snap().pane).toBeNull();
  });
});
