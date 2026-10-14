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
  it("renders PARKED as a calm neutral state with no recovery CTA", () => {
    mockHealth.mockReturnValue(
      buildHealth({ state: "PARKED", reason: "Agents asleep — they wake when you interact" }),
    );
    render(<HealthIndicator />);
    expect(screen.queryByText(/restart/i)).toBeNull();
  });


  it("HEALTHY renders nothing: the sidebar shows health only when something is wrong", () => {
    mockHealth.mockReturnValue(
      buildHealth({ state: "HEALTHY", reason: "Connected" }),
    );
    const { container } = render(<HealthIndicator />);
    expect(container).toBeEmptyDOMElement();
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
    expect(dot?.className).toContain("bg-warn");
    expect(dot?.className).toContain("animate-pulse");
    expect(screen.queryByRole("button")).toBeNull();
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
