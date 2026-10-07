// Test for ProjectsListPanel.
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const mockPost = vi.fn();
const mockMutate = vi.fn();
const mockRead = vi.fn(() => ({
  data: { projects: [{ id: "p1", name: "Marketing" }] },
  isLoading: false,
  error: null,
  mutate: mockMutate,
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: mockRead,
    post: mockPost,
    patch: vi.fn(),
    del: vi.fn(),
  }),
}));

import { ProjectsListPanel } from "@/components/teams/panels/ProjectsListPanel";

describe("ProjectsListPanel", () => {
  it("renders project rows with link to detail page", () => {
    render(<ProjectsListPanel />);
    expect(screen.getByText("Marketing")).toBeInTheDocument();
    const link = screen.getByText("Open →");
    expect(link.closest("a")).toHaveAttribute(
      "href",
      "/chat?panel=teams-project-detail&detailId=p1",
    );
  });

  it("create form posts ONLY whitelisted fields (name + optional description)", async () => {
    mockPost.mockResolvedValue({ id: "p2" });
    render(<ProjectsListPanel />);

    fireEvent.click(screen.getByText("New project"));
    fireEvent.change(screen.getByLabelText("Name"), {
      target: { value: "Sales" },
    });
    fireEvent.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(mockPost).toHaveBeenCalledWith("/projects", { name: "Sales" });
    });
    const [, body] = mockPost.mock.calls[0];
    const allowed = new Set(["name", "description"]);
    for (const k of Object.keys(body as Record<string, unknown>)) {
      expect(allowed.has(k)).toBe(true);
    }
  });
});
