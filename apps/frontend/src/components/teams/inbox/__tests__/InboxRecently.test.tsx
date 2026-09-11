import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxRecently } from "@/components/teams/inbox/InboxRecently";
import type { CompanyAgent } from "@/components/teams/shared/types";

const feed = vi.hoisted(() => ({ events: [] as unknown[], isLoading: false, isError: false }));
vi.mock("@/components/teams/dashboard/hooks/useActivityFeed", () => ({
  useActivityFeed: () => feed,
}));

const evt = (over: Record<string, unknown>) => ({
  id: "e1",
  action: "issue.created",
  entityType: "issue",
  entityId: "i1",
  createdAt: new Date().toISOString(),
  ...over,
});

beforeEach(() => {
  feed.events = [];
  feed.isLoading = false;
  feed.isError = false;
});

describe("InboxRecently", () => {
  it("is a labelled band with a deep-linkable id", () => {
    const { container } = render(<InboxRecently agents={[]} />);
    expect(container.querySelector("#inbox-recently")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Recently" })).toBeInTheDocument();
  });

  it("says nothing happened yet when the feed is empty", () => {
    render(<InboxRecently agents={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/nothing has happened yet/i);
  });

  it("groups events under a day heading", () => {
    feed.events = [evt({ id: "e1" })];
    render(<InboxRecently agents={[]} />);
    expect(screen.getByText("Today")).toBeInTheDocument();
  });

  it("names an agent actor from the roster instead of showing a raw id", () => {
    feed.events = [evt({ id: "e1", actorType: "agent", actorId: "agent_1", agentId: "agent_1" })];
    render(<InboxRecently agents={[{ id: "agent_1", name: "Friday" } as CompanyAgent]} />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.queryByText("agent_1")).toBeNull();
  });

  it("filters by type and says nothing when the filter merely empties the list", async () => {
    feed.events = [evt({ id: "e1", entityType: "issue" }), evt({ id: "e2", entityType: "agent" })];
    render(<InboxRecently agents={[]} />);
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "agent");
    expect(screen.queryByRole("status")).toBeNull();
    await userEvent.selectOptions(screen.getByLabelText("Filter by type"), "issue");
    expect(screen.getByText("Today")).toBeInTheDocument();
  });

  it("surfaces a read failure as an alert", () => {
    feed.isError = true;
    render(<InboxRecently agents={[]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t load/i);
  });
});
