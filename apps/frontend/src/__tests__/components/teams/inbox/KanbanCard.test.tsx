import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { DndContext } from "@dnd-kit/core";
import { SortableContext } from "@dnd-kit/sortable";
import { KanbanCard } from "@/components/teams/inbox/KanbanCard";
import type { Issue } from "@/components/teams/shared/types";

const baseIssue: Issue = {
  id: "iss-1",
  identifier: "ENG-42",
  title: "Wire up the kanban board",
  status: "todo",
  priority: "high",
  assigneeAgentId: null,
};

function wrap(ui: React.ReactNode) {
  return (
    <DndContext>
      <SortableContext items={["iss-1"]}>{ui}</SortableContext>
    </DndContext>
  );
}

describe("KanbanCard", () => {
  it("renders the identifier and title", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.getByText("ENG-42")).toBeInTheDocument();
    expect(screen.getByText("Wire up the kanban board")).toBeInTheDocument();
  });

  it("renders the live indicator when isLive=true", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} isLive />));
    expect(screen.getByTestId("kanban-card-live-dot")).toBeInTheDocument();
  });

  it("does not render the live indicator when isLive=false", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.queryByTestId("kanban-card-live-dot")).not.toBeInTheDocument();
  });

  it("links to the issue detail using identifier when present", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.getByRole("link")).toHaveAttribute("href", "/teams/issues/ENG-42");
  });

  it("links using id when identifier is null", () => {
    render(
      wrap(
        <KanbanCard issue={{ ...baseIssue, identifier: null }} agents={[]} />,
      ),
    );
    expect(screen.getByRole("link")).toHaveAttribute("href", "/teams/issues/iss-1");
  });

  it("renders agent name when assigneeAgentId resolves", () => {
    render(
      wrap(
        <KanbanCard
          issue={{ ...baseIssue, assigneeAgentId: "agent-1" }}
          agents={[{ id: "agent-1", name: "Atlas" }]}
        />,
      ),
    );
    expect(screen.getByText("Atlas")).toBeInTheDocument();
  });
});
