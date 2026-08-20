import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor, fireEvent } from "@testing-library/react";
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
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  constructor() {
    // Called as new WebSocket(url, protocols); extra args are ignored.
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
