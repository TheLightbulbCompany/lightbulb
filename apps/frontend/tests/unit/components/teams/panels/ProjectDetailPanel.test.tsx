// apps/frontend/tests/unit/components/teams/panels/ProjectDetailPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelTabs
// composition on the Project detail panel.

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

import { ProjectDetailPanel } from "@/components/teams/panels/ProjectDetailPanel";

describe("ProjectDetailPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { id: "p1", name: "Test Project", description: "A test" },
      isLoading: false,
      error: null,
    });

    render(<ProjectDetailPanel projectId="p1" />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /test project/i }),
    ).toBeInTheDocument();
  });

  it("renders PanelTabs with overview/issues/budget", () => {
    mockRead.mockReturnValue({
      data: { id: "p1", name: "Test Project" },
      isLoading: false,
      error: null,
    });

    render(<ProjectDetailPanel projectId="p1" />);
    expect(screen.getByRole("tab", { name: /overview/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /issues/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /budget/i })).toBeInTheDocument();
  });
});
