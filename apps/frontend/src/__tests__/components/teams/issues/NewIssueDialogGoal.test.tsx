// The composer's Goal select, and the one place it must NOT appear.
//
// Upstream's children endpoint resolves a sub-task's goal as
// `issueData.goalId ?? parent.goalId`, and the BFF forwards creates with
// `exclude_none=True` — so on a sub-task an omitted goal means "inherit the
// parent's", and an explicit null is dropped too. A "No goal" option there
// would display a choice the wire cannot honour, silently linking the sub-task
// to the parent's goal instead.
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { Goal } from "@/components/teams/shared/types";

const create = vi.fn().mockResolvedValue({ id: "iss_new" });
const createChild = vi.fn().mockResolvedValue({ id: "iss_child" });

vi.mock("@/components/teams/issues/hooks/useIssueMutations", () => ({
  useIssueMutations: () => ({ create, createChild }),
}));

import { NewIssueDialog } from "@/components/teams/issues/NewIssueDialog";

const GOALS: Goal[] = [
  { id: "goal_1", title: "Ship the thing", status: "active", level: "company" },
  { id: "goal_2", title: "Grow revenue", status: "active", level: "company" },
] as Goal[];

beforeEach(() => {
  create.mockClear();
  createChild.mockClear();
});

describe("NewIssueDialog goal selection", () => {
  it("sends the chosen goalId on a top-level create", async () => {
    render(
      <NewIssueDialog open onOpenChange={vi.fn()} goals={GOALS} />,
    );

    fireEvent.change(screen.getByLabelText(/title/i), {
      target: { value: "Fix the thing" },
    });
    fireEvent.change(screen.getByLabelText(/^goal$/i), {
      target: { value: "goal_2" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ goalId: "goal_2" });
  });

  it("omits goalId entirely when left unset", async () => {
    render(<NewIssueDialog open onOpenChange={vi.fn()} goals={GOALS} />);

    fireEvent.change(screen.getByLabelText(/title/i), {
      target: { value: "Fix the thing" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create task/i }));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0].goalId).toBeUndefined();
  });

  it("hides the Goal select for a sub-task, whose goal is inherited", () => {
    render(
      <NewIssueDialog
        open
        onOpenChange={vi.fn()}
        goals={GOALS}
        parentId="iss_parent"
      />,
    );
    expect(screen.queryByLabelText(/^goal$/i)).not.toBeInTheDocument();
    // The rest of the composer still renders — this hides one control, not the
    // sub-task flow.
    expect(screen.getByLabelText(/title/i)).toBeInTheDocument();
  });
});
