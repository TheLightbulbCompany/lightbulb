// apps/frontend/src/__tests__/teams/FailedRunRow.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Annotate the mock's return type so tsc (which type-checks test files) sees
// wakeAgent's real signature — vitest strips types at runtime, but CI runs
// `tsc --noEmit` over this file.
const wakeAgent = vi.fn<(agentId: string, opts?: { reason?: string }) => Promise<void>>();
vi.mock("@/components/teams/inbox/hooks/useAgentWake", () => ({
  useAgentWake: () => ({ wakeAgent }),
}));

import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import type { HeartbeatRun } from "@/components/teams/shared/types";

const RUN: HeartbeatRun = { id: "r1", agentId: "ag1", status: "failed", failureReason: "boom", startedAt: "2026-05-29T00:00:00Z" };

beforeEach(() => {
  wakeAgent.mockReset().mockResolvedValue(undefined);
});

describe("FailedRunRow", () => {
  it("renders the agent name + failure reason", () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
  });

  it("Dismiss calls onDismiss", () => {
    const onDismiss = vi.fn();
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    expect(onDismiss).toHaveBeenCalledOnce();
  });

  it("shows no count badge when count is 1 (default)", () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    expect(screen.queryByText(/^×\d+$/)).toBeNull();
  });

  it("shows a ×N badge when count > 1", () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} count={492} />);
    expect(screen.getByText("×492")).toBeInTheDocument();
  });

  it("uses 'heartbeat run failed' as the title when there is no failure reason", () => {
    render(
      <FailedRunRow
        run={{ id: "r3", agentId: "ag1", status: "failed" }}
        agentName="Atlas"
        onDismiss={vi.fn()}
      />,
    );
    expect(screen.getByText("Atlas — heartbeat run failed")).toBeInTheDocument();
  });

  it("Retry wakes the run's agent on demand and shows a done state", async () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    await waitFor(() => expect(wakeAgent).toHaveBeenCalledWith("ag1", { reason: "retry" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /retried/i })).toBeInTheDocument());
  });

  it("hides Retry when agentId is null (nothing to wake)", () => {
    render(<FailedRunRow run={{ id: "r2", agentId: null, status: "failed" }} agentName="?" onDismiss={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
  });

  it("renders a View link to the run-detail panel", () => {
    render(
      <FailedRunRow
        run={{ id: "run_9", agentId: "agt_1", status: "failed", startedAt: null, completedAt: null, failureReason: null }}
        agentName="Lens"
        onDismiss={() => {}}
      />,
    );
    expect(screen.getByRole("link", { name: "View run run_9" })).toHaveAttribute(
      "href",
      "/chat?panel=teams-run-detail&detailId=run_9",
    );
  });

  it("tolerates a wake failure without crashing the row", async () => {
    wakeAgent.mockRejectedValueOnce(new Error("boom"));
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));
    await waitFor(() => expect(screen.getByRole("button", { name: /retry failed/i })).toBeInTheDocument());
    // Row still renders its title — error is contained to the Retry control.
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
  });
});
