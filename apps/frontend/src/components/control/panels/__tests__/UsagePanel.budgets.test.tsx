import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { UsagePanel } from "../UsagePanel";

let budgets: Record<string, unknown> | undefined;

vi.mock("@/hooks/useBilling", () => ({
  useBilling: () => ({
    account: { subscription_status: "active" },
    isLoading: false,
    fetchUsage: async () => null,
    refresh: () => {},
  }),
}));
vi.mock("@clerk/nextjs", () => ({ useOrganization: () => ({ isLoaded: true, membership: null }) }));
vi.mock("@/hooks/useBudgets", () => ({ useBudgets: () => ({ data: budgets }) }));
vi.mock("swr", () => ({
  default: () => ({ data: undefined, error: undefined, isLoading: false, mutate: vi.fn() }),
}));

const M = 1_000_000; // microcents per dollar

describe("UsagePanel budgets", () => {
  it("shows the workspace budget's ceiling, spend and remaining", () => {
    budgets = { __default__: { ceiling_microcents: 100 * M, spent_microcents: 40 * M, month: "2026-10" } };
    render(<UsagePanel />);
    expect(screen.getByText("Workspace")).toBeInTheDocument();
    expect(screen.queryByText("__default__")).toBeNull();
    expect(screen.getByText(/\$40\.00 of \$100\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$60\.00 left/)).toBeInTheDocument();
    expect(screen.queryByText(/limit reached/i)).toBeNull();
  });

  it("shows the limit state when spend reaches the ceiling", () => {
    budgets = { proj1: { ceiling_microcents: 10 * M, spent_microcents: 12 * M, month: "2026-10" } };
    render(<UsagePanel />);
    expect(screen.getByText("proj1")).toBeInTheDocument();
    expect(screen.getByText(/limit reached/i)).toBeInTheDocument();
  });
});
