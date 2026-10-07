import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxHappeningNow } from "@/components/teams/inbox/InboxHappeningNow";

const live = vi.hoisted(() => ({ runs: [] as unknown[], isLoading: false, isError: false }));
vi.mock("@/components/teams/dashboard/hooks/useLiveRuns", async (orig) => ({
  ...(await orig<typeof import("@/components/teams/dashboard/hooks/useLiveRuns")>()),
  useLiveRuns: () => live,
}));

beforeEach(() => {
  live.runs = [];
  live.isLoading = false;
  live.isError = false;
});

describe("InboxHappeningNow", () => {
  it("is a labelled band with a deep-linkable id", () => {
    const { container } = render(<InboxHappeningNow />);
    expect(container.querySelector("#inbox-happening-now")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Happening now" })).toBeInTheDocument();
  });

  it("says nobody is running when the feed is empty", () => {
    render(<InboxHappeningNow />);
    expect(screen.getByRole("status")).toHaveTextContent(/nobody is running/i);
  });

  it("calls a silent run stalled before it calls it queued", () => {
    live.runs = [
      {
        id: "r1",
        status: "queued",
        agentName: "Atlas",
        nextAction: "Scrape leads",
        outputSilence: { level: "critical" },
      },
    ];
    render(<InboxHappeningNow />);
    expect(screen.getByText("stalled")).toBeInTheDocument();
    expect(screen.queryByText("queued")).toBeNull();
  });

  it("names the agent and what it is doing", () => {
    live.runs = [{ id: "r1", status: "running", agentName: "Friday", nextAction: "Draft the brief" }];
    render(<InboxHappeningNow />);
    expect(screen.getByText("Friday")).toBeInTheDocument();
    expect(screen.getByText("Draft the brief")).toBeInTheDocument();
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("links a run to its task when it has one, and does not invent a link when it does not", () => {
    live.runs = [
      { id: "r1", status: "running", agentName: "Friday", nextAction: "A", issueId: "i1" },
      { id: "r2", status: "running", agentName: "Atlas", nextAction: "B" },
    ];
    render(<InboxHappeningNow />);
    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute("href", "/chat?panel=teams-issue-detail&detailId=i1");
  });

  it("surfaces a read failure as an alert", () => {
    live.isError = true;
    render(<InboxHappeningNow />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t see what.s running/i);
  });
});
