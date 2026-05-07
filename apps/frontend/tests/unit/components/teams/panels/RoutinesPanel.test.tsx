// apps/frontend/tests/unit/components/teams/panels/RoutinesPanel.test.tsx
//
// Atom-contract guard: locks in the editorial PanelMasthead + PanelToolbar +
// PanelEmptyState + per-row Run-Now EditorialButton on the Routines panel.
// Behavior tests (PATCH/POST contract, checkbox toggle, create dialog) live
// in src/__tests__/teams/RoutinesPanel.test.tsx.

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();
const mockMutate = vi.fn();
const mockRead = vi.fn();

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: mockPatch,
    del: mockDel,
  }),
}));

import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";

describe("RoutinesPanel atom contract", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
    mockMutate.mockReset();
    mockRead.mockReset();
  });

  it("renders the editorial PanelMasthead with overline 'Routines' and h1 title", () => {
    mockRead.mockReturnValue({
      data: { routines: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<RoutinesPanel />);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: /routines/i }),
    ).toBeInTheDocument();
  });

  it("renders the editorial PanelToolbar search input (role=searchbox)", () => {
    mockRead.mockReturnValue({
      data: { routines: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<RoutinesPanel />);
    expect(screen.getByRole("searchbox")).toBeInTheDocument();
  });

  it("renders PanelEmptyState shape (h2 with editorial Lora-italic class) when routines list is empty", () => {
    mockRead.mockReturnValue({
      data: { routines: [] },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    render(<RoutinesPanel />);
    const headline = screen.getByRole("heading", { level: 2 });
    expect(headline).toHaveClass("font-editorial-serif");
    expect(headline).toHaveTextContent(/no routines yet/i);
  });

  it("renders a per-row 'Run now' EditorialButton that POSTs to /routines/{id}/run", async () => {
    mockRead.mockReturnValue({
      data: {
        routines: [
          {
            id: "rt1",
            name: "Daily standup",
            cron: "0 9 * * *",
            agent_id: "a1",
            prompt: "do thing",
            enabled: true,
          },
        ],
      },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });
    mockPost.mockResolvedValue({ runId: "run_1", status: "queued" });

    const user = userEvent.setup();
    render(<RoutinesPanel />);

    const runBtn = screen.getByRole("button", { name: /run.*daily standup.*now/i });
    await user.click(runBtn);

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    const [path, body] = mockPost.mock.calls[0];
    expect(path).toBe("/routines/rt1/run");
    expect(body).toEqual({ reason: "manual" });
  });

  it("filters rows by search query (case-insensitive substring match on name)", async () => {
    mockRead.mockReturnValue({
      data: {
        routines: [
          {
            id: "rt1",
            name: "Daily standup",
            cron: "0 9 * * *",
            agent_id: "a1",
            prompt: "x",
            enabled: true,
          },
          {
            id: "rt2",
            name: "Weekly review",
            cron: "0 10 * * 1",
            agent_id: "a2",
            prompt: "y",
            enabled: true,
          },
        ],
      },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    });

    const user = userEvent.setup();
    render(<RoutinesPanel />);

    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();

    const searchbox = screen.getByRole("searchbox");
    await user.type(searchbox, "weekly");

    expect(screen.queryByText("Daily standup")).not.toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();
  });
});
