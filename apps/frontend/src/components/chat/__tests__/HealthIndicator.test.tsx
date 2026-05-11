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
  it("HEALTHY renders the green dot with 'Connected' label and no recover button", () => {
    mockHealth.mockReturnValue(
      buildHealth({ state: "HEALTHY", reason: "Connected" }),
    );
    const { container } = render(<HealthIndicator />);
    expect(screen.getByText("Connected")).toBeInTheDocument();
    // Green dot. Verifying class presence covers the visual contract — the
    // test owns the green-vs-yellow-vs-red invariant, not the literal hex.
    const dot = container.querySelector("span.rounded-full");
    expect(dot?.className).toContain("bg-[#2d8a4e]");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("GATEWAY_BUSY renders a yellow pulsing dot with 'Agent thinking...' and no recover CTA", () => {
    // This is the new state introduced by the parked-vs-dead split. During a
    // long Bedrock turn the backend pushes status_change=GATEWAY_DOWN_LLM_BUSY,
    // useSystemHealth maps that to GATEWAY_BUSY, and we surface yellow rather
    // than the historical red banner. Critically there is NO Restart action
    // here — the agent is presumed alive and tearing it down would kill a
    // healthy mid-turn run.
    mockHealth.mockReturnValue(
      buildHealth({
        state: "GATEWAY_BUSY",
        reason: "Agent is still working — keeping the run alive",
        canRecover: false,
        actionLabel: null,
      }),
    );
    const { container } = render(<HealthIndicator />);
    expect(screen.getByText("Agent thinking...")).toBeInTheDocument();
    const dot = container.querySelector("span.rounded-full");
    expect(dot?.className).toContain("bg-yellow-500");
    expect(dot?.className).toContain("animate-pulse");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("GATEWAY_DOWN_DEAD renders the red dot with the reason text and a 'Restart Gateway' button", () => {
    const onRecover = vi.fn(async () => null);
    mockHealth.mockReturnValue({
      state: "GATEWAY_DOWN_DEAD",
      reason: "Gateway not responding",
      canRecover: true,
      actionLabel: "Restart Gateway",
      recover: onRecover,
      isRecovering: false,
    });
    const { container } = render(<HealthIndicator />);
    expect(screen.getByText("Gateway not responding")).toBeInTheDocument();
    const dot = container.querySelector("span.rounded-full");
    expect(dot?.className).toContain("bg-red-500");
    const btn = screen.getByRole("button", { name: "Restart Gateway" });
    expect(btn).toBeInTheDocument();
  });
});
