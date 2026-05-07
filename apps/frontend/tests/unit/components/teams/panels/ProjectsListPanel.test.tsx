// apps/frontend/tests/unit/components/teams/panels/ProjectsListPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelToolbar +
// PanelDataTable + PanelEmptyState + EditorialDialog composition on the
// Projects list panel. Behavior tests (whitelisted POST body, link href) live
// in src/__tests__/teams/ProjectsListPanel.test.tsx.

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

import { ProjectsListPanel } from "@/components/teams/panels/ProjectsListPanel";

describe("ProjectsListPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders PanelMasthead with overline + h1 title + New project CTA", () => {
    mockRead.mockReturnValue({
      data: { projects: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<ProjectsListPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /projects/i }),
    ).toBeInTheDocument();
    expect(
      screen.getAllByRole("button", { name: /new project/i }).length,
    ).toBeGreaterThan(0);
  });

  it("renders the editorial PanelToolbar search input (role=searchbox)", () => {
    mockRead.mockReturnValue({
      data: { projects: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<ProjectsListPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial-serif class) when there are no projects", () => {
    mockRead.mockReturnValue({
      data: { projects: [] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });

    render(<ProjectsListPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no projects yet/i);
  });
});
