import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor, fireEvent, within } from "@testing-library/react";
import React from "react";
import type { IDockviewPanelProps } from "dockview-react";

// Component-level render test for ScreencastDockPanel. panelComponents now maps
// the `browser` key to this panel; this test still renders the component
// directly (not through the panel key) to prove it mounts and reacts to the
// relay WS lifecycle. The pure coord/URL logic is covered separately in
// ScreencastDockPanel.test.ts.

const { getTokenMock, callRpcMock } = vi.hoisted(() => ({
  getTokenMock: vi.fn(),
  callRpcMock: vi.fn(),
}));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ getToken: getTokenMock }) }));
vi.mock("@/hooks/useGatewayRpc", () => ({ useGatewayRpcMutation: () => callRpcMock }));
vi.mock("@/lib/api", () => ({ BACKEND_URL: "https://api-dev.isol8.co/api/v1" }));

// Minimal controllable WebSocket — jsdom ships none. Captures instances so the
// test can drive open/message/close the way the backend relay would.
class MockWebSocket {
  static OPEN = 1;
  static instances: MockWebSocket[] = [];
  readyState = 0;
  url: string;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  constructor(url: string) {
    // Called as new WebSocket(url, protocols); the protocols arg is ignored, but
    // the url carries the ?target= the chrome tab strip uses to switch tabs.
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  send(data: string): void {
    this.sent.push(data);
  }
  close(): void {}
}

// jsdom ships no ResizeObserver either; capture instances so tests can fire
// the callback the way a panel-splitter drag would.
class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  cb: () => void;
  constructor(cb: () => void) {
    this.cb = cb;
    MockResizeObserver.instances.push(this);
  }
  observe(): void {}
  disconnect(): void {}
}

/** Stub the canvas's layout box (jsdom always reports 0x0). */
function setCanvasRect(width: number, height: number): HTMLCanvasElement {
  const canvas = document.querySelector("canvas") as HTMLCanvasElement;
  canvas.getBoundingClientRect = () =>
    ({ left: 0, top: 0, right: width, bottom: height, width, height, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;
  return canvas;
}

function sentResizes(ws: MockWebSocket): Array<{ width: number; height: number; deviceScaleFactor: number }> {
  return ws.sent.map((m) => JSON.parse(m)).filter((m) => m.type === "resize");
}

import { ScreencastDockPanel } from "../panels/ScreencastDockPanel";

const FRAME = JSON.stringify({
  type: "frame",
  data: "AAAA",
  metadata: { deviceWidth: 100, deviceHeight: 100 },
});

async function mountAndGetWs(): Promise<MockWebSocket> {
  render(<ScreencastDockPanel {...({} as IDockviewPanelProps)} />);
  await waitFor(() => expect(MockWebSocket.instances.length).toBe(1));
  return MockWebSocket.instances[0];
}

beforeEach(() => {
  getTokenMock.mockResolvedValue("test-token");
  callRpcMock.mockResolvedValue({});
  MockWebSocket.instances = [];
  MockResizeObserver.instances = [];
  vi.stubGlobal("WebSocket", MockWebSocket);
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ScreencastDockPanel", () => {
  it("clears the connecting overlay and shows the canvas once a frame arrives", async () => {
    const ws = await mountAndGetWs();
    expect(screen.getByText(/Connecting to your agents/)).toBeInTheDocument();

    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.();
      ws.onmessage?.({ data: FRAME });
    });

    expect(screen.queryByText(/Connecting to your agents/)).toBeNull();
    expect(document.querySelector("canvas")).not.toBeNull();
  });

  it("shows the Retry state when the relay closes", async () => {
    const ws = await mountAndGetWs();

    await act(async () => {
      ws.onclose?.();
    });

    expect(await screen.findByRole("button", { name: /Retry/ })).toBeInTheDocument();
  });

  it("sends a rounded resize (dsf 1) as soon as the relay opens", async () => {
    const ws = await mountAndGetWs();
    setCanvasRect(811.4, 622.6);

    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.();
    });

    // dsf is deliberately 1, not devicePixelRatio — pixel count is the fps
    // lever on the software-rendered container browser.
    expect(sentResizes(ws)).toEqual([{ type: "resize", width: 811, height: 623, deviceScaleFactor: 1 }]);
  });

  it("never sends a degenerate resize for a zero-sized (pre-layout) panel", async () => {
    const ws = await mountAndGetWs();
    // jsdom's default rect is 0x0 — exactly the pre-layout case.
    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.();
    });

    expect(sentResizes(ws)).toEqual([]);
  });

  it("debounces ResizeObserver bursts into one trailing resize with the latest size", async () => {
    const ws = await mountAndGetWs();
    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.(); // zero rect -> no resize yet
    });
    const observer = MockResizeObserver.instances[0];
    expect(observer).toBeDefined();

    vi.useFakeTimers();
    setCanvasRect(640, 480);
    act(() => observer.cb());
    setCanvasRect(800, 600);
    act(() => observer.cb()); // resets the 150ms trailing debounce
    act(() => {
      vi.advanceTimersByTime(200);
    });

    expect(sentResizes(ws)).toEqual([{ type: "resize", width: 800, height: 600, deviceScaleFactor: 1 }]);
  });

  it("maps pointer input by the frame's metadata, which tracks the resized viewport", async () => {
    const ws = await mountAndGetWs();
    const canvas = setCanvasRect(800, 600);

    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.();
      // The viewport now matches the panel, so metadata carries the new size.
      ws.onmessage?.({
        data: JSON.stringify({ type: "frame", data: "AAAA", metadata: { deviceWidth: 800, deviceHeight: 600 } }),
      });
    });

    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 300, button: 0, buttons: 1 });

    const mouse = ws.sent.map((m) => JSON.parse(m)).find((m) => m.type === "mouse");
    expect(mouse.event.type).toBe("mousePressed");
    // Panel center -> viewport center, 1:1 because viewport == panel size.
    expect(mouse.event.x).toBe(400);
    expect(mouse.event.y).toBe(300);
  });
});

// Browser chrome (tab strip + address bar + back/forward/reload). Drives the
// relay to connected, feeds a {type:"target"} + a /tabs list, then exercises the
// chrome. browser.request calls go through callRpcMock; a tab switch reconnects
// the screencast WS (a new MockWebSocket) carrying ?target=<raw id>.
describe("ScreencastDockPanel chrome", () => {
  const TABS = [
    { tabId: "t1", targetId: "raw-1", title: "Example", url: "https://example.com/" },
    { tabId: "t2", targetId: "raw-2", title: "Docs", url: "https://docs.example.com/" },
  ];

  function routeRpc(tabsAction: unknown = { ok: true, tab: { targetId: "raw-new", tabId: "t9" } }) {
    callRpcMock.mockImplementation((method: string, params: { path?: string }) => {
      if (method !== "browser.request") return Promise.resolve({});
      if (params?.path === "/tabs") return Promise.resolve({ running: true, tabs: TABS });
      if (params?.path === "/tabs/action") return Promise.resolve(tabsAction);
      return Promise.resolve({});
    });
  }

  async function mountConnected(rawTargetId = "raw-1"): Promise<MockWebSocket> {
    const ws = await mountAndGetWs();
    await act(async () => {
      ws.readyState = MockWebSocket.OPEN;
      ws.onopen?.();
      ws.onmessage?.({ data: JSON.stringify({ type: "target", targetId: rawTargetId }) });
      ws.onmessage?.({ data: FRAME });
    });
    await screen.findByText("Example"); // tabs loaded via refreshTabs
    return ws;
  }

  it("renders the tab strip, highlights the streamed tab, and shows its URL", async () => {
    routeRpc();
    await mountConnected("raw-1");

    expect(screen.getByText("Docs")).toBeInTheDocument();
    expect(screen.getByText("Example").closest('[role="tab"]')).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Docs").closest('[role="tab"]')).toHaveAttribute("aria-selected", "false");
    expect((screen.getByLabelText("Address bar") as HTMLInputElement).value).toBe("https://example.com/");
  });

  it("switches tabs: focuses the tab and reconnects the stream with ?target=", async () => {
    routeRpc();
    await mountConnected("raw-1");
    callRpcMock.mockClear();

    fireEvent.click(screen.getByText("Docs"));

    await waitFor(() => expect(MockWebSocket.instances.length).toBe(2));
    expect(MockWebSocket.instances[1].url).toContain("target=raw-2");
    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/tabs/focus",
      body: { targetId: "t2" },
    });
  });

  it("closes a tab by its stable alias id", async () => {
    routeRpc();
    await mountConnected("raw-1");
    callRpcMock.mockClear();

    const docsTab = screen.getByText("Docs").closest('[role="tab"]') as HTMLElement;
    await act(async () => {
      fireEvent.click(within(docsTab).getByLabelText("Close Docs"));
    });

    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "DELETE",
      path: "/tabs/t2",
    });
  });

  it("opens a new tab and streams it", async () => {
    routeRpc();
    await mountConnected("raw-1");

    fireEvent.click(screen.getByLabelText("New tab"));

    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/tabs/action",
      body: { action: "new" },
    });
    await waitFor(() => expect(MockWebSocket.instances.length).toBe(2));
    expect(MockWebSocket.instances[1].url).toContain("target=raw-new");
  });

  it("navigates to a typed URL (normalized) on the active tab", async () => {
    routeRpc();
    await mountConnected("raw-1");
    callRpcMock.mockClear();

    const input = screen.getByLabelText("Address bar") as HTMLInputElement;
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "wikipedia.org" } });
    await act(async () => {
      fireEvent.submit(input.closest("form") as HTMLFormElement);
    });

    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/navigate",
      body: { url: "https://wikipedia.org/", targetId: "t1" },
    });
  });

  it("wires back/forward to the browser tool's history.go evaluate", async () => {
    routeRpc();
    await mountConnected("raw-1");
    callRpcMock.mockClear();

    fireEvent.click(screen.getByLabelText("Back"));
    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/act",
      body: { kind: "evaluate", targetId: "t1", fn: "() => { history.go(-1); return true; }" },
    });

    callRpcMock.mockClear();
    fireEvent.click(screen.getByLabelText("Forward"));
    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/act",
      body: { kind: "evaluate", targetId: "t1", fn: "() => { history.go(1); return true; }" },
    });
  });

  it("reloads by re-navigating to the current URL", async () => {
    routeRpc();
    await mountConnected("raw-1");
    callRpcMock.mockClear();

    await act(async () => {
      fireEvent.click(screen.getByLabelText("Reload"));
    });
    expect(callRpcMock).toHaveBeenCalledWith("browser.request", {
      method: "POST",
      path: "/navigate",
      body: { url: "https://example.com/", targetId: "t1" },
    });
  });

  it("maps pointer input by the CANVAS's own rect (offset below the chrome bar)", async () => {
    routeRpc();
    const ws = await mountConnected("raw-1");

    // The canvas sits BELOW the chrome: its own rect has top=76 (the chrome
    // height). mapPointerToDevice must subtract that — using the canvas rect,
    // NOT the panel's (top=0) — or every click lands 76px too low.
    const canvas = document.querySelector("canvas") as HTMLCanvasElement;
    canvas.getBoundingClientRect = () =>
      ({ left: 0, top: 76, right: 800, bottom: 676, width: 800, height: 600, x: 0, y: 76, toJSON: () => ({}) }) as DOMRect;
    await act(async () => {
      ws.onmessage?.({
        data: JSON.stringify({ type: "frame", data: "AAAA", metadata: { deviceWidth: 800, deviceHeight: 600 } }),
      });
    });

    fireEvent.mouseDown(canvas, { clientX: 400, clientY: 76 + 300, button: 0, buttons: 1 });

    const mouse = ws.sent.map((m) => JSON.parse(m)).find((m) => m.type === "mouse");
    expect(mouse.event.x).toBe(400);
    expect(mouse.event.y).toBe(300); // 376 (client) - 76 (canvas top) = viewport center
  });
});
