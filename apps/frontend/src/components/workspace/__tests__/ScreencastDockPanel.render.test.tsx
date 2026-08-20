import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, waitFor } from "@testing-library/react";
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
  onopen: (() => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  constructor() {
    // Called as new WebSocket(url, protocols); extra args are ignored.
    MockWebSocket.instances.push(this);
  }
  send(): void {}
  close(): void {}
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
  vi.stubGlobal("WebSocket", MockWebSocket);
});

afterEach(() => {
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
});
