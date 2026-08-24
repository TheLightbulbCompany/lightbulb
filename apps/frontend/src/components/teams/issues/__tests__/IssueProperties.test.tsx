import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { IssueProperties } from "@/components/teams/issues/IssueProperties";
import type { Issue } from "@/components/teams/shared/types";

// IssueProperties' Parent/Sub-tasks rows and the IssueRelations block all read
// through useTeamsApi().read. A path-routing mock returns the right payload per
// fetched path so each row resolves independently.
type ReadResult<T> = {
  data: T | undefined;
  isLoading: boolean;
  error: null;
  mutate: ReturnType<typeof vi.fn>;
};

const PARENT_ISSUE: Issue = {
  id: "iss_parent",
  identifier: "ISO-1",
  title: "The parent epic",
  status: "in_progress",
};

const CHILDREN = {
  issues: [
    { id: "c1", title: "child one", status: "todo" },
    { id: "c2", title: "child two", status: "done" },
    { id: "c3", title: "child three", status: "todo" },
  ] as Issue[],
};

function readFor<T>(path: string): ReadResult<T> {
  const base = { isLoading: false, error: null as null, mutate: vi.fn() };
  if (path === "/issues/iss_parent") {
    return { ...base, data: PARENT_ISSUE as unknown as T };
  }
  if (path === "/issues/iss_42/children") {
    return { ...base, data: CHILDREN as unknown as T };
  }
  // IssueRelations reads /issues (candidate blockers) — empty is fine here.
  return { ...base, data: undefined };
}

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: <T,>(path: string): ReadResult<T> => readFor<T>(path),
  }),
}));

const BASE_ISSUE: Issue = {
  id: "iss_42",
  identifier: "ISO-42",
  title: "Tidy the backlog",
  status: "todo",
  priority: "high",
  parentId: "iss_parent",
  referencedIssueIdentifiers: ["ISO-5", "ISO-7"],
};

const noop = () => {};

function renderProperties(overrides: Partial<Issue> = {}) {
  return render(
    <IssueProperties
      issue={{ ...BASE_ISSUE, ...overrides }}
      onStatusChange={noop}
      onPriorityChange={noop}
      onAssigneeChange={noop}
      onProjectChange={noop}
      onBlockedByChange={noop}
    />,
  );
}

describe("IssueProperties parent / sub-tasks / related rows", () => {
  it("renders the parent as an in-shell link to the parent issue", () => {
    renderProperties();
    const link = screen.getByRole("link", { name: /ISO-1/ });
    expect(link).toHaveAttribute(
      "href",
      "/chat?panel=teams-issue-detail&detailId=iss_parent",
    );
    expect(within(link).getByText("The parent epic")).toBeInTheDocument();
  });

  it("renders the sub-tasks count (pluralized)", () => {
    renderProperties();
    expect(screen.getByText("3 sub-tasks")).toBeInTheDocument();
  });

  it("renders related-task chips from referencedIssueIdentifiers", () => {
    renderProperties();
    expect(screen.getByText("ISO-5")).toBeInTheDocument();
    expect(screen.getByText("ISO-7")).toBeInTheDocument();
  });

  it("renders an empty dash for Parent when there is no parentId", () => {
    renderProperties({ parentId: null });
    // The Parent row label is present; its value falls back to the em dash.
    const parentTerm = screen.getByText("Parent");
    const row = parentTerm.closest("div");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("—")).toBeInTheDocument();
  });

  it("orders the Labels row before the Assignee row", () => {
    renderProperties();
    const labels = screen.getByText("Labels");
    const assignee = screen.getByText("Assignee");
    // DOCUMENT_POSITION_FOLLOWING (4) means assignee comes after labels.
    expect(
      labels.compareDocumentPosition(assignee) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });
});

describe("IssueProperties monitor row", () => {
  function monitorRow() {
    return screen.getByText("Monitor").closest("div") as HTMLElement;
  }

  it("shows how far a live monitor has got, not just its next check", () => {
    renderProperties({
      executionPolicy: {
        monitor: { nextCheckAt: "2099-01-01T00:00:00Z", serviceName: "GitHub" },
      },
      executionState: {
        monitor: {
          status: "scheduled",
          attemptCount: 2,
          maxAttempts: 8,
          timeoutAt: "2099-02-01T00:00:00Z",
        },
      },
    });

    expect(within(monitorRow()).getByText(/2 checks of 8/)).toBeInTheDocument();
    // "gives up" is a FUTURE instant; it must read forwards, not "just now".
    expect(within(monitorRow()).getByText(/gives up in /)).toBeInTheDocument();
  });

  it("reads a cleared-because-triggered monitor as woken, not as a failure", () => {
    // Upstream reports a fired monitor two ways: status "triggered", or status
    // "cleared" with clearReason "triggered" once the wake consumed it.
    renderProperties({
      executionState: {
        monitor: {
          status: "cleared",
          clearReason: "triggered",
          clearedAt: "2020-01-01T00:00:00Z",
        },
      },
    });

    expect(within(monitorRow()).getByText(/Woke/)).toBeInTheDocument();
    expect(within(monitorRow()).queryByText("Not scheduled")).toBeNull();
  });

  it("does not report a fired monitor as 'Not scheduled'", () => {
    // Upstream consumes executionPolicy.monitor when the monitor fires, so the
    // policy is empty here while the wait genuinely happened.
    renderProperties({
      executionPolicy: { monitor: null },
      executionState: {
        monitor: { status: "triggered", lastTriggeredAt: "2020-01-01T00:00:00Z" },
      },
    });

    expect(within(monitorRow()).queryByText("Not scheduled")).toBeNull();
    expect(within(monitorRow()).getByText(/Woke/)).toBeInTheDocument();
  });

  it("names an exhausted wait so it is not mistaken for an idle task", () => {
    renderProperties({
      executionState: {
        monitor: { status: "cleared", clearReason: "timeout_exceeded" },
      },
    });

    expect(within(monitorRow()).getByText("Timed out")).toBeInTheDocument();
  });

  it("still reads 'Not scheduled' when no monitor was ever set", () => {
    renderProperties();
    expect(within(monitorRow()).getByText("Not scheduled")).toBeInTheDocument();
  });
});
