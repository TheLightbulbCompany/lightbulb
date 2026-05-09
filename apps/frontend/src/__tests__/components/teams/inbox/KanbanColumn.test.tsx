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

  it("collapses to compact width when empty", () => {
    const { container } = render(wrap(<KanbanColumn status="done" issues={[]} />));
    const col = container.querySelector("[data-kanban-column]");
    expect(col).toHaveAttribute("data-empty", "true");
  });

  it("does not collapse when populated", () => {
    const { container } = render(
      wrap(<KanbanColumn status="done" issues={[issue]} />),
    );
    const col = container.querySelector("[data-kanban-column]");
    expect(col).toHaveAttribute("data-empty", "false");
  });
});
