// Test for GoalsPanel — tree rendering, create body whitelist (title,
// description?, parent_id?), status chip, and EditGoalDialog patch (H1).
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

const read = vi.fn();
const patch = vi.fn().mockResolvedValue({});
const post = vi.fn().mockResolvedValue({});
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read, patch, post, del: vi.fn() }),
}));

import { GoalsPanel } from "@/components/teams/panels/GoalsPanel";

beforeEach(() => {
  read.mockReset();
  patch.mockClear();
  post.mockClear();
  post.mockResolvedValue({});
  read.mockReturnValue({
    data: { goals: [{ id: "g1", title: "Ship v1", description: "the thing", status: "planned" }] },
    isLoading: false,
    error: null,
    mutate: vi.fn(),
  });
});

describe("GoalsPanel", () => {
  it("renders tree-style goals", () => {
    read.mockReturnValue({
      data: {
        goals: [
          { id: "g1", title: "Top goal", parent_id: null },
          { id: "g2", title: "Sub goal", parent_id: "g1" },
        ],
      },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });
    render(<GoalsPanel />);
    expect(screen.getByText("Top goal")).toBeInTheDocument();
    expect(screen.getByText("Sub goal")).toBeInTheDocument();
  });

  it("create form posts whitelisted fields incl. default level + status", async () => {
    read.mockReturnValue({
      data: {
        goals: [
          { id: "g1", title: "Top goal", parent_id: null },
          { id: "g2", title: "Sub goal", parent_id: "g1" },
        ],
      },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });
    post.mockResolvedValue({ id: "g3" });
    render(<GoalsPanel />);

    fireEvent.click(screen.getByText("New goal"));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "New goal" },
    });
    fireEvent.click(screen.getByText("Create"));

    await waitFor(() => {
      // Defaults match upstream createGoalSchema (level="task", status="planned").
      expect(post).toHaveBeenCalledWith("/goals", {
        title: "New goal",
        level: "task",
        status: "planned",
      });
    });
    const [, body] = post.mock.calls[0];
    const allowed = new Set(["title", "description", "parent_id", "level", "status"]);
    for (const k of Object.keys(body as Record<string, unknown>)) {
      expect(allowed.has(k)).toBe(true);
    }
  });

  it("create form forwards chosen level + status in the post body", async () => {
    // Non-empty goals so the panel renders the list (a single "New goal"
    // masthead button) rather than the empty-state (which also renders a
    // "New goal" action, making the open-trigger lookup ambiguous).
    read.mockReturnValue({
      data: { goals: [{ id: "g1", title: "Existing", parent_id: null }] },
      isLoading: false,
      error: null,
      mutate: vi.fn(),
    });
    post.mockResolvedValue({ id: "g9" });
    render(<GoalsPanel />);

    fireEvent.click(screen.getByText("New goal"));
    fireEvent.change(screen.getByLabelText("Title"), {
      target: { value: "Company OKR" },
    });
    fireEvent.change(screen.getByLabelText("Level"), {
      target: { value: "company" },
    });
    fireEvent.change(screen.getByLabelText("Status"), {
      target: { value: "active" },
    });
    fireEvent.click(screen.getByText("Create"));

    await waitFor(() => {
      expect(post).toHaveBeenCalledWith("/goals", {
        title: "Company OKR",
        level: "company",
        status: "active",
      });
    });
  });

  it("shows a status chip for each goal", () => {
    render(<GoalsPanel />);
    expect(screen.getByText("planned")).toBeInTheDocument();
    expect(screen.getByText("Ship v1")).toBeInTheDocument();
  });

  it("opens EditGoalDialog pre-filled and patches title/description/status on save", async () => {
    const user = userEvent.setup();
    render(<GoalsPanel />);
    // Edit lives in the row's "Goal actions" dropdown (Radix menu — needs the
    // full userEvent pointer sequence, and the items portal to document.body).
    await user.click(screen.getByRole("button", { name: /goal actions/i }));
    await user.click(await screen.findByRole("menuitem", { name: /edit goal/i }));
    const title = screen.getByDisplayValue("Ship v1") as HTMLInputElement;
    expect(title).toBeInTheDocument();
    const statusSelect = screen.getByLabelText(/status/i) as HTMLSelectElement;
    fireEvent.change(statusSelect, { target: { value: "active" } });
    fireEvent.click(screen.getByRole("button", { name: /save/i }));
    await waitFor(() =>
      expect(patch).toHaveBeenCalledWith("/goals/g1", {
        title: "Ship v1",
        description: "the thing",
        status: "active",
      }),
    );
  });
});
