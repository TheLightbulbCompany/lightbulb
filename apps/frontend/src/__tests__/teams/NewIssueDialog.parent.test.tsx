// apps/frontend/src/__tests__/teams/NewIssueDialog.parent.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const create = vi.fn();
const createChild = vi.fn();
vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ create, createChild }),
}));

import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";

beforeEach(() => {
  create.mockReset().mockResolvedValue({ id: "x" });
  createChild.mockReset().mockResolvedValue({ id: "child1" });
});

describe("NewIssueDialog with parentId", () => {
  it("creates via createChild and shows the parent label", async () => {
    render(
      <NewIssueDialog open onOpenChange={() => {}} parentId="p1" parentLabel="ISO-1 Ship billing" />,
    );
    expect(screen.getByText(/ISO-1 Ship billing/)).toBeInTheDocument();
    // Placeholder is "Task title" since the Issues→Tasks rename (#1030).
    fireEvent.change(screen.getByPlaceholderText(/task title/i), { target: { value: "Sub task" } });
    fireEvent.click(screen.getByRole("button", { name: /create/i }));
    await waitFor(() => expect(createChild).toHaveBeenCalledWith("p1", expect.objectContaining({ title: "Sub task" })));
    expect(create).not.toHaveBeenCalled();
  });
});
