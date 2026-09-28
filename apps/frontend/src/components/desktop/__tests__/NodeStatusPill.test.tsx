// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { NodeStatusPill } from "../NodeStatusPill";

const gatewayState = { nodeConnected: false, nodeStatus: "disconnected", nodeStatusReason: "" };
vi.mock("@/hooks/useLiveEvents", () => ({ useLiveEvents: () => gatewayState }));

describe("NodeStatusPill", () => {
  it("renders nothing when there is no desktop node", () => {
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "disconnected";
    const { container } = render(<NodeStatusPill />);
    expect(container.firstChild).toBeNull();
  });

  it("reports a connected Mac without claiming every tool is available", () => {
    gatewayState.nodeConnected = true;
    gatewayState.nodeStatus = "connected";
    render(<NodeStatusPill />);
    expect(screen.getByText(/mac connected/i)).toBeTruthy();
  });

  it("renders a visible failing state on error instead of hiding", () => {
    // The regression this pins: an erroring node used to render NOTHING —
    // indistinguishable from no desktop app at all.
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "error";
    render(<NodeStatusPill />);
    expect(screen.getByText(/mac connection failing/i)).toBeTruthy();
  });

  it("says the Mac is not paired when the server removed its pairing", () => {
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "unpaired";
    render(<NodeStatusPill />);
    expect(screen.getByText(/mac not paired/i)).toBeTruthy();
  });

  it("shows why pairing was refused", () => {
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "unpaired";
    gatewayState.nodeStatusReason = "this Mac's keys are still paired to another workspace member";
    render(<NodeStatusPill />);
    expect(screen.getByText(/mac not paired: this mac's keys are still paired to another workspace member/i)).toBeTruthy();
    gatewayState.nodeStatusReason = "";
  });

  it("stays hidden during transient connecting/reconnecting states", () => {
    for (const status of ["connecting", "reconnecting"]) {
      gatewayState.nodeConnected = false;
      gatewayState.nodeStatus = status;
      const { container, unmount } = render(<NodeStatusPill />);
      expect(container.firstChild).toBeNull();
      unmount();
    }
  });
});
