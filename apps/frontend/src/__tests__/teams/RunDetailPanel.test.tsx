// apps/frontend/src/__tests__/teams/RunDetailPanel.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { RunDetailPanel } from "@/components/teams/panels/RunDetailPanel";

beforeEach(() => read.mockReset());

describe("RunDetailPanel", () => {
  it("prefers the heartbeat-run when it resolves", () => {
    read.mockImplementation((path: string) => {
      if (path === "/heartbeat-runs/r1") {
        return { data: { id: "r1", status: "failed" }, isLoading: false, error: null, mutate: vi.fn() };
      }
      // legacy /runs/{id} 404s for a heartbeat run
      return { data: undefined, isLoading: false, error: new Error("404"), mutate: vi.fn() };
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/Status: failed/)).toBeInTheDocument();
  });

  it("falls back to the legacy generic run when the heartbeat-run is absent", () => {
    read.mockImplementation((path: string) => {
      if (path === "/heartbeat-runs/r1") {
        return { data: undefined, isLoading: false, error: new Error("404"), mutate: vi.fn() };
      }
      return { data: { id: "r1", status: "succeeded" }, isLoading: false, error: null, mutate: vi.fn() };
    });
    render(<RunDetailPanel runId="r1" />);
    expect(screen.getByText(/Status: succeeded/)).toBeInTheDocument();
  });
});
