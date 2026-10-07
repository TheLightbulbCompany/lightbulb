// Breaker-health visibility: a routine the circuit breaker auto-paused
// (upstream sets `status: "paused"` + `autoPausedAt`) must read differently
// from a routine a human paused by hand (`status: "paused"`, no
// `autoPausedAt`) — an amber banner with the failure count + a Resume action
// that PATCHes status back to "active", reusing the same status-mutation path
// the enable/disable toggle already uses.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { SWRResponse } from "swr";
import { RoutineDetailPanel } from "../RoutineDetailPanel";
import { isAutoPaused } from "@/components/teams/shared/lib/routineHealth";

type ReadStub = Partial<SWRResponse<unknown, Error>>;
let reads: Record<string, ReadStub> = {};
const post = vi.fn();
const patch = vi.fn();
const del = vi.fn();
const mutate = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) =>
      reads[path] ?? { data: undefined, isLoading: false, error: undefined, mutate },
    post,
    patch,
    del,
  }),
}));

function seedReads(routineOverrides: Record<string, unknown> = {}) {
  reads = {
    "/routines/r1": {
      data: {
        id: "r1",
        title: "Nightly digest",
        status: "active",
        triggers: [],
        ...routineOverrides,
      },
      isLoading: false,
      error: undefined,
      mutate,
    },
    "/routines/r1/runs": { data: { runs: [] }, isLoading: false, error: undefined, mutate },
    "/routines/r1/revisions": { data: { revisions: [] }, isLoading: false, error: undefined, mutate },
    "/agents": { data: { agents: [] }, isLoading: false, error: undefined, mutate },
  };
}

beforeEach(() => {
  seedReads();
  vi.clearAllMocks();
  post.mockResolvedValue({});
  patch.mockResolvedValue({});
  del.mockResolvedValue({});
});

describe("isAutoPaused", () => {
  it("is true only when paused AND autoPausedAt is set", () => {
    expect(isAutoPaused({ status: "paused", autoPausedAt: "2026-08-10T08:00:00Z" })).toBe(true);
    expect(isAutoPaused({ status: "paused", autoPausedAt: null })).toBe(false); // hand-paused
    expect(isAutoPaused({ status: "active", autoPausedAt: null })).toBe(false);
    expect(isAutoPaused({ status: "active", autoPausedAt: "2026-08-10T08:00:00Z" })).toBe(false);
  });
});

describe("RoutineDetailPanel — auto-pause banner", () => {
  it("renders the amber banner with failure count + reason when auto-paused", () => {
    seedReads({
      status: "paused",
      autoPausedAt: "2026-08-10T08:00:00Z",
      consecutiveFailureCount: 5,
      autoPauseReason: "upstream 500",
    });
    render(<RoutineDetailPanel routineId="r1" />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveTextContent(/auto-paused after 5 failed runs/i);
    expect(banner).toHaveTextContent(/upstream 500/i);
    expect(screen.getByRole("button", { name: /resume/i })).toBeInTheDocument();
  });

  it("does not render the banner for a hand-paused routine", () => {
    seedReads({ status: "paused", autoPausedAt: null });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
  });

  it("does not render the banner for an active routine", () => {
    seedReads({ status: "active" });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("Resume PATCHes status back to active via the existing mutation path", async () => {
    const user = userEvent.setup();
    seedReads({
      status: "paused",
      autoPausedAt: "2026-08-10T08:00:00Z",
      consecutiveFailureCount: 3,
    });
    render(<RoutineDetailPanel routineId="r1" />);
    await user.click(screen.getByRole("button", { name: /resume/i }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/routines/r1", { status: "active" }));
  });

  it("Properties Status swatch is titled distinctly for auto-pause vs hand-pause", () => {
    seedReads({
      status: "paused",
      autoPausedAt: "2026-08-10T08:00:00Z",
      consecutiveFailureCount: 2,
    });
    render(<RoutineDetailPanel routineId="r1" />);
    expect(
      screen.getByTitle(/auto-paused after repeated failures/i),
    ).toBeInTheDocument();
  });
});
