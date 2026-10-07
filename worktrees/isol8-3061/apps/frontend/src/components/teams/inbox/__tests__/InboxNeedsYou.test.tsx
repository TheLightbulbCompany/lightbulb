import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { InboxNeedsYou } from "@/components/teams/inbox/InboxNeedsYou";
import type { CompanyAgent, HeartbeatRun } from "@/components/teams/shared/types";

const attention = vi.hoisted(() => ({
  value: { items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() },
}));
const triage = vi.hoisted(() => ({
  value: { failedRuns: [] as unknown[], count: 0, isLoading: false, isError: false },
}));
const acked = vi.hoisted(() => ({ ack: vi.fn() }));

vi.mock("@/components/teams/inbox/hooks/useAttentionFeed", () => ({
  useAttentionFeed: () => attention.value,
}));
vi.mock("@/components/teams/inbox/hooks/useInboxTriage", () => ({
  useInboxTriage: () => triage.value,
}));
vi.mock("@/components/teams/inbox/hooks/useAcknowledgedRuns", () => ({
  useAcknowledgedRuns: () => ({
    acknowledgedRunIds: new Set<string>(),
    isAcked: () => false,
    ack: acked.ack,
  }),
}));
vi.mock("@/components/teams/inbox/hooks/useAgentWake", () => ({
  useAgentWake: () => ({ wakeAgent: vi.fn() }),
}));

const run = (over: Partial<HeartbeatRun>): HeartbeatRun =>
  ({
    id: "r1",
    status: "failed",
    agentId: "a1",
    failureReason: "timeout",
    startedAt: "2026-09-08T10:00:00Z",
    ...over,
  }) as HeartbeatRun;

const atlas = [{ id: "a1", name: "Atlas" } as CompanyAgent];

beforeEach(() => {
  acked.ack.mockClear();
  attention.value = { items: [], count: 0, isLoading: false, isError: false, refresh: vi.fn() };
  triage.value = { failedRuns: [], count: 0, isLoading: false, isError: false };
});

describe("InboxNeedsYou", () => {
  it("is one labelled band with a section id a deep link can reach", () => {
    const { container } = render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(container.querySelector("#inbox-needs-you")).not.toBeNull();
    expect(screen.getByRole("region", { name: "Needs you" })).toBeInTheDocument();
  });

  it("says nothing needs you when both feeds are empty", () => {
    render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(screen.getByRole("status")).toHaveTextContent(/nothing needs you/i);
  });

  it("shows a failed run and does NOT also claim nothing needs you", () => {
    triage.value = { failedRuns: [run({})], count: 1, isLoading: false, isError: false };
    render(<InboxNeedsYou companyId="c1" agents={atlas} />);
    expect(screen.getByText(/timeout/i)).toBeInTheDocument();
    expect(screen.queryByText(/nothing needs you/i)).toBeNull();
  });

  it("collapses identical failures into one row and dismisses every member", async () => {
    triage.value = {
      failedRuns: [run({ id: "r1" }), run({ id: "r2" })],
      count: 2,
      isLoading: false,
      isError: false,
    };
    render(<InboxNeedsYou companyId="c1" agents={atlas} />);
    expect(screen.getByTitle("2 occurrences")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(acked.ack.mock.calls.map((c) => c[0])).toEqual(["r1", "r2"]);
  });

  it("counts both feeds in the right-hand line", () => {
    // The failed-run half is counted as ROWS RENDERED, so this seeds a real
    // run rather than a bare count — the old version passed a `count` with no
    // runs behind it, which is a state the hook cannot produce and which hid
    // the badge/rows disagreement this file now pins below.
    attention.value = { ...attention.value, count: 2 };
    triage.value = {
      failedRuns: [
        {
          id: "r1",
          status: "failed",
          agentId: "a1",
          failureReason: "timeout",
          startedAt: "2026-09-08T10:00:00Z",
        },
      ],
      count: 1,
      isLoading: false,
      isError: false,
    } as never;
    render(<InboxNeedsYou companyId="c1" agents={[{ id: "a1", name: "Atlas" } as never]} />);
    expect(screen.getByText("3 waiting")).toBeInTheDocument();
  });

  it("says so when the failed-run read fails, rather than looking clean", () => {
    triage.value = { failedRuns: [], count: 0, isLoading: false, isError: true };
    render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t check for failed runs/i);
  });

  it("does not claim nothing needs you when the failed-run read failed", () => {
    // Two feeds, one band: if /inbox/runs errors we cannot vouch for the
    // all-clear, so the alert must not sit above "Nothing needs you".
    triage.value = { failedRuns: [], count: 0, isLoading: false, isError: true } as never;
    render(<InboxNeedsYou companyId="c1" agents={[]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/couldn.t check for failed runs/i);
    expect(screen.queryByText(/nothing needs you/i)).toBeNull();
  });

  it("counts collapsed failures once, not once per occurrence", () => {
    // A flapping heartbeat emits the same failure repeatedly. The band shows
    // ONE row with a ×N badge, so the header must agree with the rows it is
    // sitting above — "492 waiting" beside a single row is the inflation the
    // grouping exists to remove.
    const flap = (id: string) => ({
      id,
      status: "failed",
      agentId: "a1",
      failureReason: "timeout",
      startedAt: "2026-09-08T10:00:00Z",
    });
    triage.value = {
      failedRuns: [flap("r1"), flap("r2"), flap("r3")],
      count: 3,
      isLoading: false,
      isError: false,
    } as never;
    attention.value = { ...attention.value, count: 2 };
    render(<InboxNeedsYou companyId="c1" agents={[{ id: "a1", name: "Atlas" } as never]} />);
    // 2 decisions + ONE collapsed failure group, not 2 + 3.
    expect(screen.getByText("3 waiting")).toBeInTheDocument();
    expect(screen.queryByText("5 waiting")).toBeNull();
  });
});
