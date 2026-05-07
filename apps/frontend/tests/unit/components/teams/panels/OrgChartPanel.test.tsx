// apps/frontend/tests/unit/components/teams/panels/OrgChartPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead composition on the
// Org chart panel. Behavior tests (loading/error/normalization) live in
// src/__tests__/components/teams/panels/OrgChartPanel.test.tsx.

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

vi.mock("@/components/teams/org-chart/OrgChart", () => ({
  OrgChart: () => <div data-testid="org-chart-stub" />,
}));

import { OrgChartPanel } from "@/components/teams/panels/OrgChartPanel";

describe("OrgChartPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { agents: [] },
      isLoading: false,
      error: null,
    });

    render(<OrgChartPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /org chart/i }),
    ).toBeInTheDocument();
  });
});
