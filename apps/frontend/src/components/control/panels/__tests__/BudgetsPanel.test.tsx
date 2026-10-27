import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BudgetsPanel } from "../BudgetsPanel";

const patch = vi.fn();
let isAdmin = true;
let overview: unknown;

// The Purchases section is dark here: its API answers 404.
vi.mock("@/lib/api", () => ({ useApi: () => ({ get: () => Promise.reject(new Error("404")), put: vi.fn() }) }));
vi.mock("@/hooks/useIsWorkspaceAdmin", () => ({ useIsWorkspaceAdmin: () => isAdmin }));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    patch,
    read: (path: string | null) => ({
      data:
        path === "/budgets/overview"
          ? overview
          : path === "/agents"
            ? { agents: [{ id: "ag1", name: "Friday" }, { id: "ag2", name: "Atlas" }] }
            : undefined,
      isLoading: false,
      mutate: vi.fn(),
    }),
  }),
}));

const policy = (scopeType: string, scopeId: string, scopeName: string, amount: number, observed: number) => ({
  scopeType,
  scopeId,
  scopeName,
  amount,
  observedAmount: observed,
  remainingAmount: Math.max(0, amount - observed),
  status: observed >= amount ? "hard_stop" : "ok",
});

describe("BudgetsPanel", () => {
  beforeEach(() => {
    patch.mockReset();
    patch.mockResolvedValue({});
    isAdmin = true;
    overview = {
      policies: [
        policy("company", "co1", "Acme", 10000, 4000),
        policy("agent", "ag1", "Friday", 2000, 2500),
      ],
    };
  });

  it("shows ceiling, spend and remaining in dollars", () => {
    render(<BudgetsPanel />);
    expect(screen.getByText(/\$40\.00 of \$100\.00/)).toBeInTheDocument();
    expect(screen.getByText(/\$60\.00 left/)).toBeInTheDocument();
  });

  it("shows the limit state for an over-budget agent", () => {
    render(<BudgetsPanel />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText(/limit reached/i)).toBeInTheDocument();
  });

  it("lists agents that have no budget yet", () => {
    render(<BudgetsPanel />);
    expect(screen.getByText("Atlas")).toBeInTheDocument();
    expect(screen.getByText(/no limit/i)).toBeInTheDocument();
  });

  it("admin sets an agent budget in dollars, PATCHed as cents", async () => {
    render(<BudgetsPanel />);
    fireEvent.change(screen.getByLabelText("Monthly budget for Atlas"), { target: { value: "25.50" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget for Atlas" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/agents/ag2/budget", { budgetMonthlyCents: 2550 }));
  });

  it("admin sets the company budget", async () => {
    render(<BudgetsPanel />);
    fireEvent.change(screen.getByLabelText("Monthly budget for Acme"), { target: { value: "200" } });
    fireEvent.click(screen.getByRole("button", { name: "Save budget for Acme" }));
    await waitFor(() => expect(patch).toHaveBeenCalledWith("/budgets", { budgetMonthlyCents: 20000 }));
  });

  it("members see spend read-only, no set-budget controls", () => {
    isAdmin = false;
    render(<BudgetsPanel />);
    expect(screen.getByText(/\$40\.00 of \$100\.00/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/Monthly budget for/)).toBeNull();
    expect(screen.queryByRole("button", { name: /Save budget/ })).toBeNull();
  });
});
