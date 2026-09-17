/**
 * M6 task 10-D, row 21. The container browser has no AgentCore equivalent:
 * once confirmed, the panel must not fire the start-probe RPC or open a VNC
 * socket the relay has no container to reach.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import React from "react";

let gate: "loading" | "hidden" | "shown";
const rpcMock = vi.hoisted(() => vi.fn(() => Promise.resolve({})));

vi.mock("@lightbulb/shared", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@lightbulb/shared")>();
  return { ...actual, useHiddenOnAgentCore: () => gate };
});
vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ getToken: () => Promise.resolve("tok") }) }));
vi.mock("@/hooks/useRuntimeRpc", () => ({ useRuntimeRpcMutation: () => rpcMock }));
vi.mock("@novnc/novnc", () => ({
  default: class FakeRFB {
    resizeSession = false;
    scaleViewport = false;
    background = "";
    addEventListener() {}
    disconnect() {}
  },
}));

import { BrowserDockPanel } from "../BrowserDockPanel";

const props = {} as unknown as Parameters<typeof BrowserDockPanel>[0];

describe("BrowserDockPanel on an AgentCore workspace", () => {
  beforeEach(() => {
    rpcMock.mockClear();
    gate = "hidden";
  });

  it("renders a notice and fires no RPC once AgentCore is confirmed", async () => {
    render(<BrowserDockPanel {...props} />);
    expect(screen.getByText(/isn.t available on this runtime/i)).toBeInTheDocument();
    // Give the (skipped) effects a tick to have fired if they were going to.
    await new Promise((r) => setTimeout(r, 0));
    expect(rpcMock).not.toHaveBeenCalled();
  });

  // Fix round 1 (review I1): the host isn't known yet for this workspace —
  // firing the start-probe (or opening a VNC socket) on a guess is exactly the
  // ~55s cold-start-to-fail this task exists to remove, so both must be held
  // exactly as they are for the confirmed-"hidden" case.
  it("holds the RPC and the VNC connect while the host isn't known yet, rather than guessing ECS", async () => {
    gate = "loading";
    render(<BrowserDockPanel {...props} />);
    expect(screen.queryByText(/isn.t available on this runtime/i)).toBeNull();
    expect(screen.getByText(/connecting to your agents/i)).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 0));
    expect(rpcMock).not.toHaveBeenCalled();
  });

  it("still starts the container browser on ECS", async () => {
    gate = "shown";
    render(<BrowserDockPanel {...props} />);
    await waitFor(() =>
      expect(rpcMock).toHaveBeenCalledWith("browser.request", { method: "POST", path: "/start" }),
    );
  });
});
