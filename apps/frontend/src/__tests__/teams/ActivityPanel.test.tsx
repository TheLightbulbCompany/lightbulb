import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({ useTeamsApi: () => ({ read }) }));

import { ActivityPanel } from "@/components/teams/panels/ActivityPanel";

beforeEach(() => {
  read.mockReset();
  read.mockReturnValue({
    data: { events: [
      { id: "e1", type: "issue.created", description: "made an issue" },
      { id: "e2", type: "run.failed", description: "a run failed" },
    ] },
    isLoading: false, error: null,
  });
});

describe("ActivityPanel", () => {
  it("renders a type filter and shows all events by default", () => {
    render(<ActivityPanel />);
    expect(screen.getByText("made an issue")).toBeInTheDocument();
    expect(screen.getByText("a run failed")).toBeInTheDocument();
    const select = screen.getByLabelText(/filter by type/i) as HTMLSelectElement;
    expect(select).toBeInTheDocument();
  });

  it("filters the list to the selected type", () => {
    render(<ActivityPanel />);
    fireEvent.change(screen.getByLabelText(/filter by type/i), { target: { value: "run.failed" } });
    expect(screen.getByText("a run failed")).toBeInTheDocument();
    expect(screen.queryByText("made an issue")).toBeNull();
  });
});
