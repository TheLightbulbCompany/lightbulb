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
