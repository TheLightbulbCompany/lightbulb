// apps/frontend/tests/unit/components/teams/panels/SkillsPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelToolbar +
// PanelEmptyState + PanelDataTable composition on the Skills panel. Skills
// remain operator-curated (no install/uninstall surface) — see the panel's
// supporting copy for the user-facing rationale.

import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: vi.fn(),
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { SkillsPanel } from "@/components/teams/panels/SkillsPanel";

describe("SkillsPanel atom contract", () => {
  beforeEach(() => {
    mockRead.mockReset();
  });

  it("renders the editorial PanelMasthead with overline + h1 title", () => {
    mockRead.mockReturnValue({
      data: { skills: [] },
      isLoading: false,
      error: null,
    });

    render(<SkillsPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /skills/i }),
    ).toBeInTheDocument();
  });

  it("renders the operator-curated supporting copy", () => {
    mockRead.mockReturnValue({
      data: { skills: [] },
      isLoading: false,
      error: null,
    });

    render(<SkillsPanel />);
    expect(screen.getByText(/operator-curated/i)).toBeInTheDocument();
  });

  it("renders the editorial PanelToolbar search input (role=searchbox)", () => {
    mockRead.mockReturnValue({
      data: { skills: [] },
      isLoading: false,
      error: null,
    });

    render(<SkillsPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial Lora-italic class) when skills list is empty", () => {
    mockRead.mockReturnValue({
      data: { skills: [] },
      isLoading: false,
      error: null,
    });

    render(<SkillsPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no skills available/i);
  });

  it("filters rows by search query (case-insensitive substring match on name + description)", async () => {
    mockRead.mockReturnValue({
      data: {
        skills: [
          {
            id: "s1",
            name: "filesystem",
            description: "read/write workspace files",
            version: "1.0.0",
          },
          {
            id: "s2",
            name: "browser",
            description: "headless Chromium automation",
            version: "0.9.1",
          },
        ],
      },
      isLoading: false,
      error: null,
    });

    const user = userEvent.setup();
    render(<SkillsPanel />);

    expect(screen.getByText("filesystem")).toBeInTheDocument();
    expect(screen.getByText("browser")).toBeInTheDocument();

    const searchbox = screen.getByRole("searchbox");
    await user.type(searchbox, "chromium");

    expect(screen.queryByText("filesystem")).not.toBeInTheDocument();
    expect(screen.getByText("browser")).toBeInTheDocument();
  });
});
