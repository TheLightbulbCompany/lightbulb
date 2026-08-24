// apps/frontend/src/__tests__/teams/NeedsAttentionBand.test.tsx
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

// Node 22+ ships a built-in `localStorage` that requires `--localstorage-file`
// to actually function. In our vitest+jsdom setup it shadows jsdom's
// localStorage and exposes a stub that throws on `.clear()` / `.setItem()`.
// Install a Map-backed polyfill on both `globalThis` and `Storage.prototype`
// so production code (the band's real useAcknowledgedRuns / loadBandCollapsed,
// which call `localStorage.{getItem,setItem,...}`) and tests (which call
// `localStorage.clear()`) both work. Mirrors the polyfill in
// useAcknowledgedRuns.test.tsx / useReadInboxItems.test.tsx / inboxStorage.test.ts.
beforeAll(() => {
  const store = new Map<string, string>();
  const polyfill: Storage = {
    getItem: (k) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k, v) => {
      store.set(k, String(v));
    },
    removeItem: (k) => {
      store.delete(k);
    },
    clear: () => {
      store.clear();
    },
    key: (i) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: polyfill,
    writable: true,
    configurable: true,
  });
  if (typeof window !== "undefined") {
    Object.defineProperty(window, "localStorage", {
      value: polyfill,
      writable: true,
      configurable: true,
    });
  }
  Storage.prototype.getItem = function (k: string) {
    return polyfill.getItem(k);
  };
  Storage.prototype.setItem = function (k: string, v: string) {
    polyfill.setItem(k, v);
  };
  Storage.prototype.removeItem = function (k: string) {
    polyfill.removeItem(k);
  };
  Storage.prototype.clear = function () {
    polyfill.clear();
  };
});

// `allFailedRuns` is the full unfiltered source; the mocked useInboxTriage
// re-derives `failedRuns` from it MINUS the acknowledged ids (just like the
// real hook) so the bulk-dismiss test can observe rows disappearing after an
// ack.
//
// Approvals are deliberately absent: they moved to the "Needs you" tab so a
// decision has exactly one home. This band is agent failures only.
const triage = {
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
      failedRuns,
      count: failedRuns.length,
      isLoading: triage.isLoading,
      isError: false,
    };
  },
}));
// Real useAcknowledgedRuns + storage are fine (jsdom localStorage).
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
  triage.allFailedRuns = [];
});

describe("NeedsAttentionBand", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<NeedsAttentionBand companyId="co1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows failed runs under an unambiguous heading; collapse hides the rows", () => {
    triage.allFailedRuns = [{ id: "r1", agentId: "ag1", status: "failed" }];
    render(
      <NeedsAttentionBand
        companyId="co1"
        agents={[{ id: "ag1", name: "Atlas" }]}
      />,
    );
    // "Agent failures", not "Needs attention" — the band no longer competes
    // with the "Needs you" tab over what the human is supposed to act on.
    expect(screen.getByText(/agent failures/i)).toBeInTheDocument();
    expect(screen.getByText(/Atlas — heartbeat run failed/)).toBeInTheDocument();
    // Collapse toggle hides the rows.
    fireEvent.click(screen.getByRole("button", { name: /agent failures/i }));
    expect(screen.queryByText(/Atlas — heartbeat run failed/)).toBeNull();
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
    expect(screen.queryByText(/agent failures/i)).toBeNull();
  });
});
