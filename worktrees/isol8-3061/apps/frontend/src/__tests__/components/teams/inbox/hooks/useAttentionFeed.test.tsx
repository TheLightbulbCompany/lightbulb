// apps/frontend/src/__tests__/components/teams/inbox/hooks/useAttentionFeed.test.tsx
//
// The badge count. Its whole job is to be TRUE — an under-reporting badge
// teaches the owner the surface can be ignored, which is the failure this
// feature exists to fix.

import { describe, test, expect, vi, beforeEach } from "vitest";
import { renderHook } from "@testing-library/react";

const read = vi.fn();
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({ read, post: vi.fn(), patch: vi.fn(), del: vi.fn() }),
}));
vi.mock("swr", async () => {
  const actual = await vi.importActual<typeof import("swr")>("swr");
  return { ...actual, useSWRConfig: () => ({ mutate: vi.fn() }) };
});

import { useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";

function installFeed(feed: unknown) {
  read.mockImplementation((path: string) =>
    path === "/attention"
      ? { data: feed, isLoading: false, error: null, mutate: vi.fn() }
      : { data: undefined, isLoading: false, error: null, mutate: vi.fn() },
  );
}

function item(id: string, sourceKind: string) {
  return { id, sourceKind, subject: { id, kind: sourceKind } };
}

beforeEach(() => vi.clearAllMocks());

describe("useAttentionFeed", () => {
  test("counts from pre-pagination source counts, not the page", () => {
    // Upstream defaults to a 50-row page but builds countsBySourceKind from
    // the FULL ranked set. A 51st pending decision must still be badged.
    installFeed({
      items: Array.from({ length: 50 }, (_, n) => item(`a${n}`, "approval")),
      countsBySourceKind: { approval: 51 },
    });
    const { result } = renderHook(() => useAttentionFeed());
    expect(result.current.items).toHaveLength(50);
    expect(result.current.count).toBe(51);
  });

  test("sums every decision kind", () => {
    installFeed({
      items: [],
      countsBySourceKind: {
        approval: 1,
        issue_thread_interaction: 9,
        blocker_attention: 2,
      },
    });
    expect(renderHook(() => useAttentionFeed()).result.current.count).toBe(12);
  });

  test("excludes health kinds from the count", () => {
    // failed_run / budget_alert report what happened; nothing waits on you.
    installFeed({
      items: [],
      countsBySourceKind: { approval: 1, failed_run: 40, budget_alert: 3 },
    });
    expect(renderHook(() => useAttentionFeed()).result.current.count).toBe(1);
  });

  test("a genuinely empty feed counts zero", () => {
    installFeed({ items: [], countsBySourceKind: { approval: 0 } });
    expect(renderHook(() => useAttentionFeed()).result.current.count).toBe(0);
  });

  test("falls back to the page length when counts are absent", () => {
    // Under-reports rather than inventing a number — the right direction to
    // be wrong in for a badge.
    installFeed({ items: [item("a1", "approval")] });
    expect(renderHook(() => useAttentionFeed()).result.current.count).toBe(1);
  });

  test("a malformed body is zero, never a guess", () => {
    installFeed(undefined);
    const { result } = renderHook(() => useAttentionFeed());
    expect(result.current.count).toBe(0);
    expect(result.current.items).toEqual([]);
  });
});
