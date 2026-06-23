import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// ActivityPanel reuses `useActivityFeed`, which reads `/activity` via
// `useTeamsApi().read`. We mock the BFF read to return the real wire shape —
// a BARE ARRAY of events with the correct fields (action/actorType/actorId/
// entityType/entityId), which is what the live endpoint returns. The previous
// `{ events: [...] }` envelope + `type/description` fields never matched
// production, which is the bug this panel rebuild fixes.
const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { ActivityPanel } from "@/components/teams/panels/ActivityPanel";

beforeEach(() => {
  read.mockReset();
  read.mockReturnValue({
    data: [
      {
        id: "e1",
        action: "issue.created",
        actorType: "agent",
        actorId: "Friday",
        entityType: "issue",
        entityId: "ISS-1",
        createdAt: new Date().toISOString(),
      },
      {
        id: "e2",
        action: "agent.terminated",
        actorType: "user",
        actorId: "Alice",
        entityType: "agent",
        entityId: "agent-xyz",
        createdAt: new Date().toISOString(),
      },
    ],
    isLoading: false,
    error: null,
  });
});

describe("ActivityPanel", () => {
  it("renders the normalized bare-array events with the shared ActivityRow", () => {
    render(<ActivityPanel />);
    // Actor identities (rendered by ActivityRow via Identity).
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("Alice")).toBeInTheDocument();
    // Verbs mapped from `action`.
    expect(screen.getByText("created")).toBeInTheDocument();
    expect(screen.getByText("terminated")).toBeInTheDocument();
    // Entity ids rendered as the entity hint.
    expect(screen.getByText("ISS-1")).toBeInTheDocument();
    expect(screen.getByText("agent-xyz")).toBeInTheDocument();
  });

  it("renders a type filter and shows all events by default", () => {
    render(<ActivityPanel />);
    const select = screen.getByLabelText(/filter by type/i) as HTMLSelectElement;
    expect(select).toBeInTheDocument();
    expect(select.value).toBe("all");
  });

  it("filters the list by entityType", () => {
    render(<ActivityPanel />);
    fireEvent.change(screen.getByLabelText(/filter by type/i), {
      target: { value: "issue" },
    });
    // Only the issue event survives the entityType facet.
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("ISS-1")).toBeInTheDocument();
    expect(screen.queryByText("Alice")).toBeNull();
    expect(screen.queryByText("agent-xyz")).toBeNull();
  });

  it("shows the empty state when there are no events", () => {
    read.mockReturnValue({ data: [], isLoading: false, error: null });
    render(<ActivityPanel />);
    expect(screen.getByText("No activity yet.")).toBeInTheDocument();
  });
});
