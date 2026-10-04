import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import type { DockviewApi } from "dockview-react";
import {
  attachDockviewApi,
  openPanel,
  setTasksPlacement,
  showBrowserBesideChat,
  browserSplitWidth,
  refitBrowserSplit,
  refitSplits,
  setBrowserThreadHost,
  useBrowserThreadHost,
  _resetControllerForTests,
  _snapshotForTests,
} from "../workspaceController";
import { MESSAGES_PANEL_ID } from "../panelRegistry";
import { TASKS_PLACEMENT_KEY, readTasksPlacement } from "../layoutStorage";

vi.mock("@/lib/shellNav", () => ({ pushSearchParams: vi.fn(), replaceSearchParams: vi.fn() }));
import { pushSearchParams } from "@/lib/shellNav";

interface FakeGroup { id: string; api: { width: number; setSize: ReturnType<typeof vi.fn> } }
const fakeGroup = (id: string, width = 0): FakeGroup => ({ id, api: { width, setSize: vi.fn() } });
interface FakePanel {
  id: string;
  params: Record<string, string>;
  title: string;
  group: FakeGroup;
  api: {
    setActive: ReturnType<typeof vi.fn>;
    updateParameters: ReturnType<typeof vi.fn>;
    setTitle: ReturnType<typeof vi.fn>;
    moveTo: ReturnType<typeof vi.fn>;
  };
}

// Just enough of dockview to see WHERE a panel lands: a panel added with a
// position (or moved with a non-center position) gets its own group.
function makeApi({ withMessages = true, width = 1400 } = {}) {
  const main = fakeGroup("main", width);
  const panels = new Map<string, FakePanel>();
  let active: FakePanel | undefined;
  let splits = 0;
  function add(id: string, group: FakeGroup, params: Record<string, string> = {}): FakePanel {
    const p: FakePanel = {
      id,
      params,
      title: id,
      group,
      api: {
        setActive: vi.fn(() => { active = p; }),
        updateParameters: vi.fn(),
        setTitle: vi.fn(),
        moveTo: vi.fn((o: { group: FakeGroup; position?: string }) => {
          p.group = !o.position || o.position === "center" ? o.group : fakeGroup(`split-${++splits}`);
        }),
      },
    };
    panels.set(id, p);
    active = p;
    return p;
  }
  if (withMessages) add(MESSAGES_PANEL_ID, main, { agentId: "a1" });
  const api = {
    width,
    getPanel: (id: string) => panels.get(id),
    addPanel: vi.fn((o: { id: string; params?: Record<string, string>; position?: unknown }) =>
      add(o.id, o.position ? fakeGroup(`split-${++splits}`) : main, o.params)),
    onDidActivePanelChange: () => ({ dispose() {} }),
    onDidAddPanel: () => ({ dispose() {} }),
    onDidRemovePanel: () => ({ dispose() {} }),
    onDidLayoutChange: () => ({ dispose() {} }),
  };
  Object.defineProperty(api, "panels", { get: () => [...panels.values()] });
  Object.defineProperty(api, "activePanel", { get: () => active });
  Object.defineProperty(api, "groups", {
    get: () => [...new Set([...panels.values()].map((p) => p.group.id))].map((gid) => ({
      activePanel: gid === active?.group.id ? active : [...panels.values()].find((p) => p.group.id === gid),
    })),
  });
  return { api: api as unknown as DockviewApi & { addPanel: ReturnType<typeof vi.fn> }, main };
}

beforeEach(() => {
  _resetControllerForTests();
  window.localStorage.clear();
  vi.clearAllMocks();
});
afterEach(() => vi.restoreAllMocks());

describe("Tasks placement", () => {
  it("opens Tasks split to the right of Messages by default, keeping the URL push", () => {
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues", {}, { pushUrl: true });
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({
      id: "teams-issues",
      position: { referencePanel: MESSAGES_PANEL_ID, direction: "right" },
    }));
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    // The conversation stays visible beside it.
    expect(_snapshotForTests().messagesVisible).toBe(true);
  });

  it("focuses an existing Tasks panel where it is instead of adding or moving it", () => {
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    openPanel("teams-issues");
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    const tasks = api.getPanel("teams-issues") as unknown as FakePanel;
    expect(tasks.api.setActive).toHaveBeenCalled();
    expect(tasks.api.moveTo).not.toHaveBeenCalled();
  });

  it("Expand moves Tasks into the Messages group as a tab and remembers it", () => {
    const { api, main } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    setTasksPlacement("tab");
    const tasks = api.getPanel("teams-issues") as unknown as FakePanel;
    expect(tasks.api.moveTo).toHaveBeenCalledWith({ group: main, position: "center" });
    expect(tasks.group).toBe(main);
    expect(window.localStorage.getItem(TASKS_PLACEMENT_KEY)).toBe("tab");
    setTasksPlacement("split");
    expect(tasks.api.moveTo).toHaveBeenLastCalledWith({ group: main, position: "right" });
    expect(readTasksPlacement()).toBe("split");
  });

  it("folds a Tasks split into a tab when the dock is too narrow for both, keeping the stored choice", () => {
    const { api, main } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    const tasks = api.getPanel("teams-issues") as unknown as FakePanel;
    expect(tasks.group).not.toBe(main);
    // Not laid out yet: leave the split alone.
    (api as unknown as { width: number }).width = 0;
    refitSplits();
    expect(tasks.api.moveTo).not.toHaveBeenCalled();
    // A phone-width dock (a wide window's saved layout, restored or resized).
    (api as unknown as { width: number }).width = 390;
    refitSplits();
    expect(tasks.api.moveTo).toHaveBeenCalledWith({ group: main, position: "center" });
    expect(tasks.group).toBe(main);
    expect(readTasksPlacement()).toBe("split");
    // Wide again: the stored choice is still split, so Tasks goes back beside.
    (api as unknown as { width: number }).width = 1400;
    refitSplits();
    expect(tasks.api.moveTo).toHaveBeenLastCalledWith({ group: main, position: "right" });
    expect(tasks.group).not.toBe(main);
    // Settled: another resize moves nothing.
    refitSplits();
    expect(tasks.api.moveTo).toHaveBeenCalledTimes(2);
  });

  it("leaves Tasks a tab on a wide dock when the person chose Expand", () => {
    const { api, main } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    setTasksPlacement("tab");
    const tasks = api.getPanel("teams-issues") as unknown as FakePanel;
    tasks.api.moveTo.mockClear();
    refitSplits();
    expect(tasks.api.moveTo).not.toHaveBeenCalled();
    expect(tasks.group).toBe(main);
  });

  it("honours a persisted tab choice on the next open", () => {
    window.localStorage.setItem(TASKS_PLACEMENT_KEY, "tab");
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    expect(api.addPanel.mock.calls[0][0]).not.toHaveProperty("position");
  });

  it("falls back to split when storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("denied"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("denied"); });
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    expect(() => setTasksPlacement("tab")).not.toThrow();
    expect(readTasksPlacement()).toBe("split");
  });

  it("opens as a tab when Messages is not in the layout", () => {
    const { api } = makeApi({ withMessages: false });
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    expect(api.addPanel.mock.calls[0][0]).not.toHaveProperty("position");
  });

  it("opens as a tab when the dock is too narrow to split", () => {
    const { api } = makeApi({ width: 600 });
    attachDockviewApi(api, () => "a1");
    openPanel("teams-issues");
    expect(api.addPanel.mock.calls[0][0]).not.toHaveProperty("position");
  });

  it("leaves other destinations as tabs", () => {
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("teams-inbox");
    expect(api.addPanel.mock.calls[0][0]).not.toHaveProperty("position");
  });
});

describe("Browser beside the chat", () => {
  it("opens beside Messages on a wide dock and never moves or refocuses an open panel unasked", () => {
    const { api } = makeApi();
    attachDockviewApi(api, () => "a1");
    expect(showBrowserBesideChat()).toBe(true);
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({
      id: "browser",
      position: { referencePanel: MESSAGES_PANEL_ID, direction: "right" },
    }));
    expect(_snapshotForTests().messagesVisible).toBe(true);
    const browser = api.getPanel("browser") as unknown as FakePanel;
    browser.api.setActive.mockClear();
    expect(showBrowserBesideChat()).toBe(true);
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    expect(browser.api.setActive).not.toHaveBeenCalled();
    showBrowserBesideChat({ focus: true });
    expect(browser.api.setActive).toHaveBeenCalled();
  });

  it("on a narrow dock, opens only when asked, as a tab", () => {
    const { api } = makeApi({ width: 500 });
    attachDockviewApi(api, () => "a1");
    expect(showBrowserBesideChat()).toBe(false);
    expect(api.addPanel).not.toHaveBeenCalled();
    expect(showBrowserBesideChat({ focus: true, asTabWhenNarrow: true })).toBe(true);
    expect(api.addPanel).toHaveBeenCalledWith(expect.not.objectContaining({ position: expect.anything() }));
  });

  it("brings a Browser tab kept behind Messages out beside the conversation", () => {
    const { api, main } = makeApi();
    attachDockviewApi(api, () => "a1");
    openPanel("browser"); // a restored layout: Browser as a tab in the Messages group
    const browser = api.getPanel("browser") as unknown as FakePanel;
    expect(browser.group).toBe(main);
    expect(showBrowserBesideChat()).toBe(true);
    expect(browser.api.moveTo).toHaveBeenCalledWith({ group: main, position: "right" });
    expect(browser.group).not.toBe(main);
    expect(_snapshotForTests().messagesVisible).toBe(true);
  });
});

describe("Browser and room: two columns", () => {
  it.each([
    [1280, 640], // the browser's floor; the room keeps 640
    [1600, 800],
    [2000, 1000],
  ])("a %i px dock gives the browser %i px and the room the rest (>= 560)", (dock, browser) => {
    expect(browserSplitWidth(dock)).toBe(browser);
    expect(dock - browserSplitWidth(dock)).toBeGreaterThanOrEqual(560);
    const { api } = makeApi({ width: dock });
    attachDockviewApi(api, () => "a1");
    expect(showBrowserBesideChat()).toBe(true);
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({ initialWidth: browser }));
  });

  it("never splits a dock too narrow for both columns", () => {
    const { api } = makeApi({ width: 1100 });
    attachDockviewApi(api, () => "a1");
    expect(showBrowserBesideChat()).toBe(false);
    expect(api.addPanel).not.toHaveBeenCalled();
  });

  it("folds the Browser behind the conversation when the window shrinks below both minimums", () => {
    const { api, main } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    showBrowserBesideChat();
    const browser = api.getPanel("browser") as unknown as FakePanel;
    (api as unknown as { width: number }).width = 1100;
    refitBrowserSplit();
    expect(browser.api.moveTo).toHaveBeenCalledWith({ group: main, position: "center" });
    expect(_snapshotForTests().messagesVisible).toBe(true);
  });

  it("gives a squeezed column its split back", () => {
    const { api } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    showBrowserBesideChat();
    const browser = api.getPanel("browser") as unknown as FakePanel;
    browser.group.api.width = 500;
    refitBrowserSplit();
    expect(browser.group.api.setSize).toHaveBeenCalledWith({ width: 800 });
  });
});

describe("thread under the browser", () => {
  const slot = document.createElement("div");
  afterEach(() => setBrowserThreadHost(null));

  it("thread opened while the browser is beside the chat: it renders in the browser's slot", () => {
    const { api } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    showBrowserBesideChat();
    setBrowserThreadHost(slot);
    const { result } = renderHook(() => useBrowserThreadHost());
    expect(result.current).toBe(slot);
  });

  it("browser opened while a thread is open: the thread moves under it", () => {
    const { api } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    const { result } = renderHook(() => useBrowserThreadHost());
    expect(result.current).toBeNull(); // no browser: the thread stays beside the room
    act(() => {
      showBrowserBesideChat();
      setBrowserThreadHost(slot);
    });
    expect(result.current).toBe(slot);
  });

  it("browser closed: the thread goes back beside the room", () => {
    const { api } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    showBrowserBesideChat();
    setBrowserThreadHost(slot);
    const { result } = renderHook(() => useBrowserThreadHost());
    act(() => setBrowserThreadHost(null)); // the panel unmounts its slot
    expect(result.current).toBeNull();
  });

  it("a Browser tab behind the conversation never takes the thread", () => {
    const { api } = makeApi({ width: 1600 });
    attachDockviewApi(api, () => "a1");
    openPanel("browser"); // a tab in the Messages group
    act(() => (api.getPanel(MESSAGES_PANEL_ID) as unknown as FakePanel).api.setActive());
    setBrowserThreadHost(slot);
    const { result } = renderHook(() => useBrowserThreadHost());
    expect(result.current).toBeNull();
  });
});
