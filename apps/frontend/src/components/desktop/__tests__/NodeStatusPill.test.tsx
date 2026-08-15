// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";

import { NodeStatusPill } from "../NodeStatusPill";

const gatewayState = { nodeConnected: false, nodeStatus: "disconnected" };
vi.mock("@/hooks/useGateway", () => ({ useGateway: () => gatewayState }));

describe("NodeStatusPill", () => {
  it("renders nothing when there is no desktop node", () => {
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "disconnected";
    const { container } = render(<NodeStatusPill />);
    expect(container.firstChild).toBeNull();
  });

  it("renders the available pill when connected", () => {
    gatewayState.nodeConnected = true;
    gatewayState.nodeStatus = "connected";
    render(<NodeStatusPill />);
    expect(screen.getByText(/local tools available/i)).toBeTruthy();
  });

  it("renders a visible failing state on error instead of hiding", () => {
    // The regression this pins: an erroring node used to render NOTHING —
    // indistinguishable from no desktop app at all.
    gatewayState.nodeConnected = false;
    gatewayState.nodeStatus = "error";
    render(<NodeStatusPill />);
    expect(screen.getByText(/local tools: connection failing/i)).toBeTruthy();
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
