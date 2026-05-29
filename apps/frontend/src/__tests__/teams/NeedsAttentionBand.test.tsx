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

const triage = { approvals: [] as unknown[], failedRuns: [] as unknown[], count: 0, isLoading: false };
vi.mock("@/components/teams/inbox/hooks/useInboxTriage", () => ({
  useInboxTriage: () => triage,
}));
// Real useAcknowledgedRuns + storage are fine (jsdom localStorage). Stub the
// mutations the rows pull in so no network is touched.
vi.mock("@/components/teams/inbox/hooks/useApprovalMutations", () => ({
  useApprovalMutations: () => ({ approve: vi.fn(), reject: vi.fn() }),
}));

import { NeedsAttentionBand } from "@/components/teams/inbox/NeedsAttentionBand";

beforeEach(() => {
  localStorage.clear();
  triage.approvals = [];
  triage.failedRuns = [];
  triage.count = 0;
});

describe("NeedsAttentionBand", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<NeedsAttentionBand companyId="co1" agents={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows count + both row types; collapse hides the rows", () => {
    triage.approvals = [{ id: "a1", title: "Deploy?", status: "pending" }];
    triage.failedRuns = [{ id: "r1", agentId: "ag1", status: "failed" }];
    triage.count = 2;
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
});
