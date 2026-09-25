import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import type { Issue } from "@/components/teams/shared/types";

// The agent-scope filter (from the /chat roster's "See her work" affordance) is
// now a prop, not a URL read. The dismiss chip reports the clear back to the
// shell via onAgentFilterCleared instead of committing a URL strip itself.

// `/issues` returns a mixed-assignee set; `/agents` resolves the chip name.
const ISSUES: Issue[] = [
  { id: "iss_a", title: "Alpha task", status: "open", assigneeAgentId: "agent_friday" },
  { id: "iss_b", title: "Bravo task", status: "open", assigneeAgentId: "agent_atlas" },
  { id: "iss_c", title: "Charlie task", status: "open", assigneeAgentId: null },
];

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => {
      // Prefix-match: the panel's issues key carries the routine-filter query
      // string (`/issues?excludeRoutineExecutions=true`) by default.
      if (path.startsWith("/issues"))
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

const activityState = vi.hoisted(() => ({ items: [] as unknown[] }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => activityState }));
const navigateShell = vi.hoisted(() => vi.fn());
vi.mock("@/lib/shellNav", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/shellNav")>()),
  navigateShell,
}));

import { IssuesPanel } from "../IssuesPanel";

beforeEach(() => {
  vi.clearAllMocks();
});

describe("IssuesPanel agent filter (agentFilter prop)", () => {
  it("shows only the matching agent's issues when agentFilter is set", () => {
    render(<IssuesPanel agentFilter="agent_friday" />);
    expect(screen.getByText("Alpha task")).toBeInTheDocument();
    expect(screen.queryByText("Bravo task")).not.toBeInTheDocument();
    expect(screen.queryByText("Charlie task")).not.toBeInTheDocument();
  });

  it("shows all issues when agentFilter is absent", () => {
    render(<IssuesPanel />);
    expect(screen.getByText("Alpha task")).toBeInTheDocument();
    expect(screen.getByText("Bravo task")).toBeInTheDocument();
    expect(screen.getByText("Charlie task")).toBeInTheDocument();
  });

  it("renders a dismissible filter chip resolving the agent name when agentFilter is set", () => {
    render(<IssuesPanel agentFilter="agent_friday" />);
    // Chip text resolves the roster name rather than the raw id. Scope the
    // match to the chip's "Filtered to …" copy — a plain /Friday/ now also
    // matches the per-row assignee chip (rows surface the assignee name).
    expect(screen.getByText(/Filtered to Friday/)).toBeInTheDocument();
  });

  it("does not render the filter chip when agentFilter is absent", () => {
    render(<IssuesPanel />);
    expect(screen.queryByLabelText(/clear agent filter/i)).not.toBeInTheDocument();
  });

  it("dismiss chip reports the clear via onAgentFilterCleared (the shell owns the URL strip)", () => {
    const onAgentFilterCleared = vi.fn();
    render(
      <IssuesPanel
        agentFilter="agent_friday"
        onAgentFilterCleared={onAgentFilterCleared}
      />,
    );
    fireEvent.click(screen.getByLabelText(/clear agent filter/i));
    expect(onAgentFilterCleared).toHaveBeenCalledTimes(1);
  });
});

describe("IssuesPanel approvals entry", () => {
  it("shows pending approvals and opens Activity on Needs you", () => {
    activityState.items = [
      { id: "paperclip:approval:a1", kind: "paperclip", subtype: "approval", state: "open" },
      { id: "paperclip:approval:a2", kind: "paperclip", subtype: "approval", state: "open" },
      { id: "paperclip:approval:a3", kind: "paperclip", subtype: "approval", state: "resolved" },
      { id: "mention:1", kind: "mention", subtype: null, state: "open" },
    ];
    render(<IssuesPanel />);
    const entry = screen.getByRole("button", { name: "Approvals, 2 waiting on you" });
    fireEvent.click(entry);
    expect(navigateShell).toHaveBeenCalledWith("/chat?panel=activity&filter=needs-you");
    activityState.items = [];
  });
});
