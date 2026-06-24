// apps/frontend/src/__tests__/teams/FailedRunRow.test.tsx
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FailedRunRow } from "@/components/teams/inbox/FailedRunRow";
import type { HeartbeatRun } from "@/components/teams/shared/types";

const RUN: HeartbeatRun = { id: "r1", agentId: "ag1", status: "failed", failureReason: "boom", startedAt: "2026-05-29T00:00:00Z" };

describe("FailedRunRow", () => {
  it("renders agent name + failure reason and View links to the run detail", () => {
    render(<FailedRunRow run={RUN} agentName="Atlas" onDismiss={vi.fn()} />);
    expect(screen.getByText(/Atlas/)).toBeInTheDocument();
    expect(screen.getByText(/boom/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /view/i })).toHaveAttribute(
      "href",
      "/teams/agents/ag1/runs/r1",
    );
  });

  it("hides View when agentId is null", () => {
    render(<FailedRunRow run={{ id: "r2", agentId: null, status: "failed" }} agentName="?" onDismiss={vi.fn()} />);
    expect(screen.queryByRole("link", { name: /view/i })).toBeNull();
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
});
