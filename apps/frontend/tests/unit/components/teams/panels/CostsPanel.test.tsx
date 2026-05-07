// apps/frontend/tests/unit/components/teams/panels/CostsPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + KPI card +
// PanelDataTable composition on the Costs panel. Period filters + budgets
// are deliberately out of scope (BFF surface returns total/by_agent/by_project
// only — see plan 2026-05-07-teams-costs-atoms-port.md).

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { CostsPanel } from "@/components/teams/panels/CostsPanel";

describe("CostsPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title + period meta", () => {
    mockRead.mockReturnValue({
      data: {
        total_cents: 1234,
        period: "April 2026",
        by_agent: [{ agent_id: "a1", agent_name: "Goose", cents: 800 }],
        by_project: [],
      },
      isLoading: false,
      error: null,
    });

    render(<CostsPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /costs/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/april 2026/i)).toBeInTheDocument();
  });

  it("renders the total in a KPI card", () => {
    mockRead.mockReturnValue({
      data: {
        total_cents: 1234,
        period: "April 2026",
        by_agent: [{ agent_id: "a1", agent_name: "Goose", cents: 800 }],
        by_project: [],
      },
      isLoading: false,
      error: null,
    });

    render(<CostsPanel />);
    expect(screen.getByText(/\$12\.34/)).toBeInTheDocument();
  });

  it("renders the by-agent breakdown via PanelDataTable", () => {
    mockRead.mockReturnValue({
      data: {
        total_cents: 1234,
        period: "April 2026",
        by_agent: [{ agent_id: "a1", agent_name: "Goose", cents: 800 }],
        by_project: [],
      },
      isLoading: false,
      error: null,
    });

    render(<CostsPanel />);
    expect(screen.getByText("Goose")).toBeInTheDocument();
    expect(screen.getByText(/\$8\.00/)).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial serif class) when there is no spend", () => {
    mockRead.mockReturnValue({
      data: {
        total_cents: 0,
        by_agent: [],
        by_project: [],
      },
      isLoading: false,
      error: null,
    });

    render(<CostsPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no spend recorded/i);
  });
});
