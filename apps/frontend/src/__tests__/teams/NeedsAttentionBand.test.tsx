// apps/frontend/src/__tests__/teams/NeedsAttentionBand.test.tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// `allFailedRuns` is the full unfiltered source; the mocked useInboxTriage
// re-derives `failedRuns` from it MINUS the acknowledged ids (just like the
// real hook) so the bulk-dismiss test can observe rows disappearing after an
// ack. `approvals` stays a plain array (approvals aren't grouped/acked).
const triage = {
  approvals: [] as {
    id: string;
    status: string;
    type?: string;
    payload?: Record<string, unknown>;
  }[],
  allFailedRuns: [] as {
    id: string;
    agentId?: string | null;
    failureReason?: string | null;
    status: string;
  }[],
  isLoading: false,
};
vi.mock("@/components/teams/inbox/hooks/useInboxTriage", () => ({
  useInboxTriage: (acknowledgedRunIds: Set<string>) => {
    const failedRuns = triage.allFailedRuns.filter((r) => !acknowledgedRunIds.has(r.id));
    return {
      approvals: triage.approvals,
      failedRuns,
      count: triage.approvals.length + failedRuns.length,
      isLoading: triage.isLoading,
      isError: false,
    };
  },
}));
// Real useAcknowledgedRuns + storage are fine (jsdom localStorage). Stub the
// mutations the rows pull in so no network is touched.
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve: vi.fn(), reject: vi.fn() }),
}));
// FailedRunRow's Retry pulls in useAgentWake; stub it so the band test stays
// network-free (typed so tsc accepts wakeAgent's signature).
vi.mock("@/components/teams/inbox/hooks/useAgentWake", () => ({
  useAgentWake: () => ({
    wakeAgent: vi.fn<(agentId: string, opts?: { reason?: string }) => Promise<void>>(),
  }),
}));

import { NeedsAttentionBand } from "@/components/teams/inbox/NeedsAttentionBand";

beforeEach(() => {
  localStorage.clear();
  triage.approvals = [];
  triage.allFailedRuns = [];
});

describe("NeedsAttentionBand", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<NeedsAttentionBand companyId="co1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows count + both row types; collapse hides the rows", () => {
    // The approval label is synthesized from `type` + `payload` (upstream
    // never sends a top-level `title`), so drive the visible label via the
    // payload subject → "Board Approval: Deploy?".
    triage.approvals = [
      { id: "a1", status: "pending", type: "request_board_approval", payload: { title: "Deploy?" } },
    ];
    triage.allFailedRuns = [{ id: "r1", agentId: "ag1", status: "failed" }];
    render(
      <NeedsAttentionBand
        companyId="co1"
        agents={[{ id: "ag1", name: "Atlas" }]}
      />,
    );
    expect(screen.getByText(/needs attention/i)).toBeInTheDocument();
    expect(screen.getByText(/Deploy\?/)).toBeInTheDocument();
    expect(screen.getByText(/Atlas — heartbeat run failed/)).toBeInTheDocument();
    // Collapse toggle hides the rows.
    fireEvent.click(screen.getByRole("button", { name: /needs attention/i }));
    expect(screen.queryByText(/Deploy\?/)).toBeNull();
  });

  it("collapses identical failures (same agent + reason) into one row with a ×N badge", () => {
    // 3 occurrences of the SAME (agentId, failureReason) — distinct run ids
    // + startedAt (per-occurrence). They must collapse to a single row.
    triage.allFailedRuns = [
      { id: "r1", agentId: "ag1", failureReason: "boom", status: "failed" },
      { id: "r2", agentId: "ag1", failureReason: "boom", status: "failed" },
      { id: "r3", agentId: "ag1", failureReason: "boom", status: "failed" },
    ];
    render(
      <NeedsAttentionBand companyId="co1" agents={[{ id: "ag1", name: "Atlas" }]} />,
    );
    // Exactly one failed-run row title (failure reason is part of it).
    expect(screen.getAllByText("Atlas — boom")).toHaveLength(1);
    expect(screen.getByText("×3")).toBeInTheDocument();
  });

  it("keeps distinct failure reasons (same agent) as separate rows", () => {
    triage.allFailedRuns = [
      { id: "r1", agentId: "ag1", failureReason: "boom", status: "failed" },
      { id: "r2", agentId: "ag1", failureReason: "kaboom", status: "failed" },
    ];
    render(
      <NeedsAttentionBand companyId="co1" agents={[{ id: "ag1", name: "Atlas" }]} />,
    );
    expect(screen.getByText("Atlas — boom")).toBeInTheDocument();
    expect(screen.getByText("Atlas — kaboom")).toBeInTheDocument();
    // No badge — each group is a single occurrence.
    expect(screen.queryByText(/^×\d+$/)).toBeNull();
  });

  it("bulk-dismiss acks every member of a group so the row clears", () => {
    triage.allFailedRuns = [
      { id: "r1", agentId: "ag1", failureReason: "boom", status: "failed" },
      { id: "r2", agentId: "ag1", failureReason: "boom", status: "failed" },
      { id: "r3", agentId: "ag1", failureReason: "boom", status: "failed" },
    ];
    render(
      <NeedsAttentionBand companyId="co1" agents={[{ id: "ag1", name: "Atlas" }]} />,
    );
    expect(screen.getByText("×3")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /dismiss/i }));
    // All three ids acked → the (mocked) triage drops them → row gone →
    // band renders nothing.
    expect(screen.queryByText("Atlas — boom")).toBeNull();
    // The whole band collapses to empty (count hit 0).
    expect(screen.queryByText(/needs attention/i)).toBeNull();
  });
});
