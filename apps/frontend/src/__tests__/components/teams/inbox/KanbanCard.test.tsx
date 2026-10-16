import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { DndContext } from "@dnd-kit/core";
import { SortableContext } from "@dnd-kit/sortable";
import { KanbanCard, KanbanCardOverlay } from "@/components/teams/inbox/KanbanCard";
import { InlineTeamsNavProvider } from "@/components/chat/InlineTeamsNav";
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

  it("links to the issue detail by id (not identifier — BFF expects id)", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    // baseIssue has identifier "ENG-42", but the detailId must use id "iss-1"
    // so the BFF /teams/issues/{id} → upstream /api/issues/{id} fetch resolves.
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss-1",
    );
  });

  it("disables native anchor drag so dnd-kit can drive the sort", () => {
    // Native anchors are browser-draggable by default, which hijacks the
    // pointer and prevents dnd-kit's PointerSensor from starting the sortable
    // drag — so cards "move but don't stay." draggable={false} is the fix.
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute("draggable", "false");
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss-1",
    );
  });

  it("shows the assigned agent as a named avatar", () => {
    render(
      wrap(
        <KanbanCard
          issue={{ ...baseIssue, assigneeAgentId: "agent-1" }}
          agents={[{ id: "agent-1", name: "Atlas" }]}
        />,
      ),
    );
    expect(screen.getByRole("img", { name: "Atlas" })).toHaveAttribute("title", "Atlas");
  });

  it("shows a human assignee by their member name (assigneeUserId is the Paperclip id)", () => {
    render(
      wrap(
        <KanbanCard
          issue={{ ...baseIssue, assigneeUserId: "pc-user-1" }}
          agents={[]}
          members={[{ userId: "user_clerk", paperclipUserId: "pc-user-1", name: "Leo Park" }]}
        />,
      ),
    );
    expect(screen.getByRole("img", { name: "Leo Park" })).toHaveTextContent("LP");
  });

  it("marks an unassigned task with an empty circle", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.getByRole("img", { name: "Unassigned" })).toBeInTheDocument();
  });

  it("has no per-card status picker — dragging changes status", () => {
    render(wrap(<KanbanCard issue={baseIssue} agents={[]} />));
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });
});

describe("KanbanCardOverlay", () => {
  it("renders without DndContext (no useSortable registration)", () => {
    // The overlay must NOT call useSortable — otherwise it would double-
    // register the active card's id and confuse dnd-kit's collision
    // detection. Rendering without a DndContext wrapper proves the
    // component is presentational-only.
    render(<KanbanCardOverlay issue={baseIssue} agents={[]} />);
    expect(screen.getByText("ENG-42")).toBeInTheDocument();
    expect(screen.getByText("Wire up the kanban board")).toBeInTheDocument();
  });

  it("links by id (consistent with KanbanCard)", () => {
    render(<KanbanCardOverlay issue={baseIssue} agents={[]} />);
    expect(screen.getByRole("link")).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss-1",
    );
  });

  it("carries a data-kanban-card-overlay attribute (not -id) so it doesn't collide with the active card in queries", () => {
    const { container } = render(<KanbanCardOverlay issue={baseIssue} agents={[]} />);
    expect(container.querySelector(`[data-kanban-card-overlay="iss-1"]`)).toBeInTheDocument();
    expect(container.querySelector(`[data-kanban-card-id="iss-1"]`)).not.toBeInTheDocument();
  });
});

describe("Kanban card inline-shell routing", () => {
  // Rendered via the overlay (no dnd-kit / isDragging) — it shares CardContent
  // with the sortable KanbanCard, so it exercises the same onClick.
  const linkOf = (c: HTMLElement) =>
    c.querySelector(
      'a[href="/chat?panel=teams-issue-detail&detailId=iss-1"]',
    ) as HTMLAnchorElement;

  it("routes a plain click through openDetail when inside the nav shell", () => {
    const openDetail = vi.fn();
    const { container } = render(
      <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
        <KanbanCardOverlay issue={baseIssue} agents={[]} />
      </InlineTeamsNavProvider>,
    );
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    linkOf(container).dispatchEvent(click);
    expect(openDetail).toHaveBeenCalledWith("issue-detail", "iss-1");
    expect(click.defaultPrevented).toBe(true);
  });

  it("keeps native link behavior for a modified click (new tab)", () => {
    const openDetail = vi.fn();
    const { container } = render(
      <InlineTeamsNavProvider value={{ openDetail, leaveToList: vi.fn() }}>
        <KanbanCardOverlay issue={baseIssue} agents={[]} />
      </InlineTeamsNavProvider>,
    );
    const cmdClick = new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      metaKey: true,
    });
    linkOf(container).dispatchEvent(cmdClick);
    expect(openDetail).not.toHaveBeenCalled();
    expect(cmdClick.defaultPrevented).toBe(false);
  });

  it("is not intercepted without a nav provider (href drives navigation)", () => {
    const { container } = render(
      <KanbanCardOverlay issue={baseIssue} agents={[]} />,
    );
    const click = new MouseEvent("click", { bubbles: true, cancelable: true });
    linkOf(container).dispatchEvent(click);
    expect(click.defaultPrevented).toBe(false);
  });
});
