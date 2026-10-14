// @vitest-environment jsdom
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { HealthIndicator } from "../HealthIndicator";
import type { HealthState, SystemHealth } from "@/hooks/useSystemHealth";

// Mock useSystemHealth at the module boundary so we can exercise each
// state branch in isolation without standing up the WS / SWR scaffolding.
const mockHealth = vi.fn();
vi.mock("@/hooks/useSystemHealth", async (importOriginal) => {
  const real = (await importOriginal()) as Record<string, unknown>;
  return {
    ...real,
    useSystemHealth: () => mockHealth(),
  };
});

function buildHealth(overrides: Partial<SystemHealth> & { state: HealthState }): SystemHealth {
  return {
    state: overrides.state,
    reason: overrides.reason ?? "",
    canRecover: overrides.canRecover ?? false,
    actionLabel: overrides.actionLabel ?? null,
    recover: vi.fn(async () => null),
    isRecovering: false,
  };
}

describe("HealthIndicator", () => {
  // The sidebar shows health only when something is broken: normal, asleep,
  // starting and mid-turn are all fine, so they take no space.
  it.each(["HEALTHY", "PARKED", "STARTING", "GATEWAY_BUSY"] as const)("%s renders nothing", (state) => {
    mockHealth.mockReturnValue(buildHealth({ state, reason: "fine" }));
    const { container } = render(<HealthIndicator />);
    expect(container).toBeEmptyDOMElement();
  });

  it("RECOVERING renders its one line with the reason", () => {
    mockHealth.mockReturnValue(buildHealth({ state: "RECOVERING", reason: "Reconnecting live updates" }));
    render(<HealthIndicator />);
    expect(screen.getByText("Reconnecting live updates")).toBeInTheDocument();
  });

  it("CONTAINER_DOWN renders the red dot with the reason text and a 'Restart Agent' button", () => {
    const onRecover = vi.fn(async () => null);
    mockHealth.mockReturnValue({
      state: "CONTAINER_DOWN",
      reason: "Agents are not running",
      canRecover: true,
      actionLabel: "Restart Agent",
      recover: onRecover,
      isRecovering: false,
    });
    const { container } = render(<HealthIndicator />);
    expect(screen.getByText("Agents are not running")).toBeInTheDocument();
    const dot = container.querySelector("span.rounded-full");
    expect(dot?.className).toContain("bg-err");
    const btn = screen.getByRole("button", { name: "Restart Agent" });
    expect(btn).toBeInTheDocument();
  });
});
