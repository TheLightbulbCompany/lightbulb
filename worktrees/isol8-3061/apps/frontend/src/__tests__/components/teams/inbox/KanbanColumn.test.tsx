import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { DndContext } from "@dnd-kit/core";
import { KanbanColumn } from "@/components/teams/inbox/KanbanColumn";
import type { Issue } from "@/components/teams/shared/types";

const issue: Issue = {
  id: "iss-1",
  identifier: "ENG-1",
  title: "Test issue",
  status: "todo",
  priority: null,
};

function wrap(ui: React.ReactNode) {
  return <DndContext>{ui}</DndContext>;
}

describe("KanbanColumn", () => {
  it("renders the column label (status with underscores → spaces, title-cased)", () => {
    const { getByText } = render(wrap(<KanbanColumn status="in_progress" issues={[issue]} />));
    expect(getByText(/In Progress/i)).toBeInTheDocument();
  });

  it("renders the count badge with the number of issues", () => {
    const { getByText } = render(
      wrap(
        <KanbanColumn
          status="todo"
          issues={[issue, { ...issue, id: "iss-2", identifier: "ENG-2" }]}
        />,
      ),
    );
    expect(getByText("2")).toBeInTheDocument();
  });

  it("renders all issues in the column", () => {
    const { getByText } = render(
      wrap(
        <KanbanColumn
          status="todo"
          issues={[issue, { ...issue, id: "iss-2", identifier: "ENG-2" }]}
        />,
      ),
    );
    expect(getByText("ENG-1")).toBeInTheDocument();
    expect(getByText("ENG-2")).toBeInTheDocument();
  });

  it("keeps an empty column readable — shows its label and a 0 count", () => {
    // Empty workflow stages must NOT collapse to a label-less sliver:
    // that hid the column name and broke dragging cards into it.
    const { getByText } = render(
      wrap(<KanbanColumn status="in_review" issues={[]} />),
    );
    expect(getByText(/In Review/i)).toBeInTheDocument();
    expect(getByText("0")).toBeInTheDocument();
  });
});
