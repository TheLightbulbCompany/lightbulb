import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { Issue } from "@/components/teams/shared/types";

// `?agent=<id>` deep link from the /chat roster's "See her work" affordance.
// `mockReplace` lets us assert the dismiss chip strips only the `agent` param.
// These override tests/setup.ts's global next/navigation mock so we can drive
// the panel with a specific search string + a spyable router.
const { mockReplace, searchParamsRef } = vi.hoisted(() => ({
  mockReplace: vi.fn(),
  searchParamsRef: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: mockReplace,
    prefetch: vi.fn(),
    back: vi.fn(),
  }),
  usePathname: () => "/chat",
  useSearchParams: () => searchParamsRef.current,
}));

// `/issues` returns a mixed-assignee set; `/agents` resolves the chip name.
const ISSUES: Issue[] = [
  { id: "iss_a", title: "Alpha task", status: "open", assigneeAgentId: "agent_friday" },
  { id: "iss_b", title: "Bravo task", status: "open", assigneeAgentId: "agent_atlas" },
  { id: "iss_c", title: "Charlie task", status: "open", assigneeAgentId: null },
];

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      if (path === "/issues")
        return { data: { issues: ISSUES }, isLoading: false, error: null, mutate: vi.fn() };
      if (path === "/agents")
        return {
          data: { agents: [{ id: "agent_friday", name: "Friday" }] },
          isLoading: false,
          error: null,
          mutate: vi.fn(),
        };
      return { data: undefined, isLoading: false, error: null, mutate: vi.fn() };
    },
  }),
}));

vi.mock("@/components/teams/issues/IssueStatusMenu", () => ({
  IssueStatusMenu: () => null,
}));
vi.mock("@/components/teams/issues/NewIssueDialog", () => ({
  NewIssueDialog: () => null,
}));

import { IssuesPanel } from "../IssuesPanel";

beforeEach(() => {
  vi.clearAllMocks();
  searchParamsRef.current = new URLSearchParams();
});

describe("IssuesPanel agent filter (?agent)", () => {
  it("shows only the matching agent's issues when ?agent is set", () => {
    searchParamsRef.current = new URLSearchParams("agent=agent_friday");
    render(<IssuesPanel />);
    expect(screen.getByText("Alpha task")).toBeInTheDocument();
    expect(screen.queryByText("Bravo task")).not.toBeInTheDocument();
    expect(screen.queryByText("Charlie task")).not.toBeInTheDocument();
  });

  it("shows all issues when ?agent is absent", () => {
    render(<IssuesPanel />);
    expect(screen.getByText("Alpha task")).toBeInTheDocument();
    expect(screen.getByText("Bravo task")).toBeInTheDocument();
    expect(screen.getByText("Charlie task")).toBeInTheDocument();
  });

  it("renders a dismissible filter chip resolving the agent name when ?agent is set", () => {
    searchParamsRef.current = new URLSearchParams("agent=agent_friday");
    render(<IssuesPanel />);
    // Chip text resolves the roster name rather than the raw id.
    expect(screen.getByText(/Friday/)).toBeInTheDocument();
  });

  it("does not render the filter chip when ?agent is absent", () => {
    render(<IssuesPanel />);
    expect(screen.queryByLabelText(/clear agent filter/i)).not.toBeInTheDocument();
  });

  it("dismiss chip clears ONLY the agent param (preserving others) via router.replace", () => {
    searchParamsRef.current = new URLSearchParams(
      "panel=teams-issues&agent=agent_friday&view=control",
    );
    render(<IssuesPanel />);
    fireEvent.click(screen.getByLabelText(/clear agent filter/i));
    expect(mockReplace).toHaveBeenCalledTimes(1);
    const target = mockReplace.mock.calls[0][0] as string;
    expect(target).not.toMatch(/agent=/);
    expect(target).toMatch(/panel=teams-issues/);
    expect(target).toMatch(/view=control/);
  });
});
