import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { BlockedTasksView } from "@/components/teams/issues/BlockedTasksView";
import { defaultIssueFilterState } from "@/components/teams/shared/lib/issueFilters";
import type {
  Issue,
  IssueBlockedInboxAttention,
  IssueBlockedInboxReason,
  IssueBlockedInboxSeverity,
} from "@/components/teams/shared/types";

const { readSpy, readResult } = vi.hoisted(() => ({
  readSpy: vi.fn(),
  readResult: { current: {} as Record<string, unknown> },
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string) => (readSpy(path), readResult.current),
  }),
}));

const NOW = Date.now();
const ago = (ms: number) => new Date(NOW - ms).toISOString();
const HOUR = 3_600_000;

function attention(
  reason: IssueBlockedInboxReason,
  severity: IssueBlockedInboxSeverity,
  stoppedSinceAt: string | null,
  owner: Partial<IssueBlockedInboxAttention["owner"]> = {},
): IssueBlockedInboxAttention {
  return {
    kind: "blocked",
    state: "needs_attention",
    reason,
    severity,
    stoppedSinceAt,
    owner: { type: "agent", agentId: null, userId: null, label: null, ...owner },
    action: { label: "Do the thing", detail: null },
    sourceIssue: null,
    leafIssue: null,
    recoveryIssue: null,
    approvalId: null,
    interactionId: null,
    sampleIssueIdentifier: null,
    redaction: { externalDetailsRedacted: false, secretFieldsOmitted: true },
  };
}

function issue(id: string, title: string, a: IssueBlockedInboxAttention | null, updatedAt?: string): Issue {
  return { id, identifier: id.toUpperCase(), title, status: "blocked", blockedInboxAttention: a, updatedAt };
}

const AGENTS = [{ id: "agent-friday", name: "Friday" }];

function renderView(props: Partial<Parameters<typeof BlockedTasksView>[0]> = {}) {
  return render(
    <BlockedTasksView
      searchQuery=""
      agents={AGENTS}
      members={[]}
      groupBy="blocker_type"
      sortBy="urgency"
      issueFilters={defaultIssueFilterState}
      currentUserId={null}
      {...props}
    />,
  );
}

function titles(): string[] {
  return screen.getAllByRole("link").map((el) => el.textContent?.match(/Task [a-z]+/)?.[0] ?? "");
}

beforeEach(() => {
  vi.clearAllMocks();
  readResult.current = { data: { issues: [] }, error: undefined, isLoading: false };
});

describe("BlockedTasksView", () => {
  it("fetches the company's blocked list once with the attention params", () => {
    renderView();
    expect(readSpy).toHaveBeenCalledWith(
      "/issues?attention=blocked&includeBlockedInboxAttention=true&includeBlockedBy=true&includeLiveDescendantSummary=true&limit=200",
    );
  });

  it("scopes the fetch to a project when given one", () => {
    renderView({ projectId: "proj 1" });
    expect(readSpy.mock.calls[0][0]).toMatch(/&project=proj%201$/);
  });

  it('empty state "No work is stopped."', () => {
    renderView();
    expect(screen.getByText("No work is stopped.")).toBeInTheDocument();
  });

  it("rows show reason chip, owner and stopped-age", () => {
    readResult.current = {
      data: {
        issues: [
          issue("iso-1", "Task one", attention("pending_board_decision", "high", ago(3 * HOUR), { agentId: "agent-friday" })),
        ],
      },
      isLoading: false,
    };
    renderView();
    const row = screen.getByRole("link");
    expect(within(row).getAllByTestId("blocked-reason-chip")[0]).toHaveAttribute(
      "aria-label",
      "Reason: Needs decision, severity high",
    );
    expect(within(row).getAllByText("Friday").length).toBeGreaterThan(0);
    expect(within(row).getAllByText("stopped 3h").length).toBeGreaterThan(0);
  });

  it("groups Needs decision / Blocked chain stalled / Recovery required / External wait / Owner paused", () => {
    readResult.current = {
      data: {
        issues: [
          issue("iso-5", "Task paused", attention("blocked_by_uninvokable_assignee", "low", ago(HOUR))),
          issue("iso-4", "Task external", attention("external_owner_action", "low", ago(HOUR))),
          issue("iso-3", "Task recovery", attention("open_recovery_issue", "low", ago(HOUR))),
          issue("iso-2", "Task stalled", attention("blocked_chain_stalled", "low", ago(HOUR))),
          issue("iso-1", "Task decision", attention("pending_user_decision", "low", ago(HOUR))),
          issue("iso-0", "Task not stopped", null),
        ],
      },
      isLoading: false,
    };
    renderView();
    const headers = screen.getAllByRole("button").map((b) => b.textContent);
    expect(headers).toEqual([
      "Needs decision · 1",
      "Blocked chain stalled · 1",
      "Recovery required · 1",
      "External wait · 1",
      "Owner paused · 1",
    ]);
    expect(screen.queryByText("Task not stopped")).toBeNull();
  });

  it("sorts by Most urgent, Most recent, Longest stopped", () => {
    readResult.current = {
      data: {
        issues: [
          // most urgent, stopped in between
          issue("iso-1", "Task critical", attention("pending_board_decision", "critical", ago(8 * HOUR))),
          // low severity, stopped longest
          issue("iso-2", "Task old", attention("pending_board_decision", "low", ago(10 * HOUR))),
          // medium, stopped most recently
          issue("iso-3", "Task mid", attention("pending_board_decision", "medium", ago(5 * HOUR))),
        ],
      },
      isLoading: false,
    };
    const { rerender } = renderView({ groupBy: "none", sortBy: "urgency" });
    expect(titles()).toEqual(["Task critical", "Task mid", "Task old"]);

    const props = {
      searchQuery: "",
      agents: AGENTS,
      members: [],
      groupBy: "none" as const,
      issueFilters: defaultIssueFilterState,
      currentUserId: null,
    };
    rerender(<BlockedTasksView {...props} sortBy="most_recent" />);
    expect(titles()).toEqual(["Task mid", "Task critical", "Task old"]);

    rerender(<BlockedTasksView {...props} sortBy="longest_stopped" />);
    expect(titles()).toEqual(["Task old", "Task critical", "Task mid"]);
  });

  it("narrows by the search box", () => {
    readResult.current = {
      data: {
        issues: [
          issue("iso-1", "Task alpha", attention("pending_board_decision", "low", ago(HOUR))),
          issue("iso-2", "Task beta", attention("pending_board_decision", "low", ago(HOUR))),
        ],
      },
      isLoading: false,
    };
    renderView({ searchQuery: "beta", groupBy: "none" });
    expect(titles()).toEqual(["Task beta"]);
  });

  it("keeps the panel's agent filter", () => {
    readResult.current = {
      data: {
        issues: [
          { ...issue("iso-1", "Task alpha", attention("pending_board_decision", "low", ago(HOUR))), assigneeAgentId: "a1" },
          { ...issue("iso-2", "Task beta", attention("pending_board_decision", "low", ago(HOUR))), assigneeAgentId: "a2" },
        ],
      },
      isLoading: false,
    };
    renderView({ agentFilter: "a2", groupBy: "none" });
    expect(titles()).toEqual(["Task beta"]);
  });
});
