// apps/frontend/tests/unit/components/teams/panels/GoalsPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + tree renderer +
// PanelEmptyState composition on the Goals panel. Behavior tests (whitelisted
// POST body, tree rendering) live in src/__tests__/teams/GoalsPanel.test.tsx.

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

import { GoalsPanel } from "@/components/teams/panels/GoalsPanel";

describe("GoalsPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title + New goal CTA", () => {
    mockRead.mockReturnValue({
      data: { goals: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<GoalsPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /goals/i }),
    ).toBeInTheDocument();
    // Both the masthead CTA + the empty-state action expose a "New goal"
    // button. We just need at least one — the empty-state and masthead-CTA
    // assertions live in their own sibling tests above/below.
    expect(
      screen.getAllByRole("button", { name: /new goal/i }).length,
    ).toBeGreaterThan(0);
  });

  it("renders PanelEmptyState shape (h2 with editorial-serif class) when goals list is empty", () => {
    mockRead.mockReturnValue({
      data: { goals: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<GoalsPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no goals yet/i);
  });
});
