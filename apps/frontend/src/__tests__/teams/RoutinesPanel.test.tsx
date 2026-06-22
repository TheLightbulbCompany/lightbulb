// Test for RoutinesPanel — confirms create body matches CreateRoutineBody and
// the per-row enable toggle posts a PATCH with only {enabled}.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mockPost = vi.fn();
const mockPatch = vi.fn();
const mockDel = vi.fn();
const mockMutate = vi.fn();
const mockRead = vi.fn(() => ({
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
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: mockPatch,
    del: mockDel,
  }),
}));

import { RoutinesPanel } from "@/components/teams/panels/RoutinesPanel";

describe("RoutinesPanel", () => {
  beforeEach(() => {
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
    // Restore the default single-row read for the base suite (the filter
    // suite below swaps in a multi-agent set per test).
    mockRead.mockImplementation(() => ({
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
    }));
  });

  it("renders routine rows", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText(/0 9 \* \* \*/)).toBeInTheDocument();
  });

  it("renders an Open link to the routine detail page", () => {
    render(<RoutinesPanel />);
    expect(screen.getByRole("link", { name: /open/i })).toHaveAttribute(
      "href",
      "/teams/routines/rt1",
    );
  });

  it("toggle posts PATCH with only {enabled: false}", async () => {
    mockPatch.mockResolvedValue({});
    render(<RoutinesPanel />);

    fireEvent.click(screen.getByRole("checkbox"));

    await waitFor(() => {
      expect(mockPatch).toHaveBeenCalledTimes(1);
    });
    expect(mockPatch).toHaveBeenCalledWith("/routines/rt1", { enabled: false });
    const [, body] = mockPatch.mock.calls[0];
    expect(Object.keys(body as Record<string, unknown>)).toEqual(["enabled"]);
  });

  it("create form posts whitelisted fields only", async () => {
    mockPost.mockResolvedValue({ id: "rt2" });
    render(<RoutinesPanel />);

    fireEvent.click(screen.getByText("New routine"));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Weekly review" },
    });
    fireEvent.change(screen.getByLabelText("Cron"), {
      target: { value: "0 10 * * 1" },
    });
    fireEvent.change(screen.getByLabelText("Agent ID"), {
      target: { value: "a1" },
    });
    fireEvent.change(screen.getByLabelText("Prompt"), {
      target: { value: "review last week" },
    });
    fireEvent.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledTimes(1);
    });
    const [path, body] = mockPost.mock.calls[0];
    expect(path).toBe("/routines");
    const allowed = new Set(["name", "cron", "agent_id", "prompt", "enabled"]);
    for (const k of Object.keys(body as Record<string, unknown>)) {
      expect(allowed.has(k)).toBe(true);
    }
  });
});

describe("RoutinesPanel agentId filter (inspector intrinsic scope)", () => {
  // A mixed-agent set: two for `a1`, one for `a2`. The inspector passes its
  // inspected agent's id; only that agent's routines should render. Unlike the
  // standalone `/teams` panel, the filter is intrinsic — NO dismissible chip.
  beforeEach(() => {
    mockPost.mockReset();
    mockPatch.mockReset();
    mockDel.mockReset();
    mockRead.mockImplementation(() => ({
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
          {
            id: "rt2",
            name: "Weekly review",
            cron: "0 10 * * 1",
            agent_id: "a1",
            prompt: "review",
            enabled: true,
          },
          {
            id: "rt3",
            name: "Nightly backup",
            cron: "0 2 * * *",
            agent_id: "a2",
            prompt: "backup",
            enabled: true,
          },
        ],
      },
      isLoading: false,
      error: null,
      mutate: mockMutate,
    }));
  });

  it("renders only the given agent's routines when agentId is set", () => {
    render(<RoutinesPanel agentId="a1" />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();
    expect(screen.queryByText("Nightly backup")).not.toBeInTheDocument();
  });

  it("renders all routines when agentId is absent (standalone unchanged)", () => {
    render(<RoutinesPanel />);
    expect(screen.getByText("Daily standup")).toBeInTheDocument();
    expect(screen.getByText("Weekly review")).toBeInTheDocument();
    expect(screen.getByText("Nightly backup")).toBeInTheDocument();
  });

  it("does NOT render a dismissible filter chip when agentId is set", () => {
    render(<RoutinesPanel agentId="a1" />);
    expect(
      screen.queryByLabelText(/clear agent filter/i),
    ).not.toBeInTheDocument();
  });
});
