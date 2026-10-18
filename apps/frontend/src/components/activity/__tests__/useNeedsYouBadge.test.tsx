import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, renderHook, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ActivityItem } from "@lightbulb/shared";
import { needsYouBadge, useNeedsYouBadge } from "../useNeedsYouBadge";
import { AttentionDesk } from "@/components/teams/inbox/AttentionDesk";
import { isAttentionKey } from "@/components/teams/inbox/hooks/useAttentionFeed";
import type { AttentionItem, HeartbeatRun } from "@/components/teams/shared/types";
import { approval, message } from "./fixtures";

const { feeds, activity, mutate, post } = vi.hoisted(() => ({
  feeds: { current: {} as Record<string, unknown> },
  activity: { current: { attentionCount: 0, items: [] as unknown[] } },
  mutate: vi.fn(),
  post: vi.fn(),
}));

vi.mock("@clerk/nextjs", () => ({ useAuth: () => ({ userId: "user_1", orgId: "org_1" }) }));
vi.mock("@/hooks/useActivity", () => ({ useActivity: () => activity.current }));
vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => ({ data: path ? feeds.current[path] : undefined, isLoading: false, error: undefined, mutate: vi.fn() }),
    post,
    del: vi.fn().mockResolvedValue(undefined),
    mutate,
  }),
}));
vi.mock("@/lib/notify", () => ({ notifySuccess: vi.fn(), notifyApiError: vi.fn() }));

const NOW = "2026-10-18T15:00:00.000Z";

function deskItem(id: string, over: Partial<AttentionItem> = {}): AttentionItem {
  return {
    id, sourceKind: "approval", whyNow: "why", severity: "medium", activityAt: NOW, createdAt: NOW, relatedIssue: null, originAgentName: null,
    subject: { kind: "approval", id: `ap-${id}`, title: `Title ${id}`, identifier: null, status: null, href: null },
    dismissalKey: `attention:${id}`, dismissal: null, ...over,
  };
}

function run(id: string, agentId: string): HeartbeatRun {
  return { id, agentId, status: "failed", failureReason: `boom ${agentId}`, startedAt: NOW } as HeartbeatRun;
}

const failedRunActivity = { ...approval, id: "paperclip:failed_run:r9", subtype: "failed_run" } as ActivityItem;

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  feeds.current = {};
  activity.current = { attentionCount: 0, items: [] };
  post.mockResolvedValue({});
});

describe("needsYouBadge", () => {
  it("swaps the server's Paperclip asks for the desk count and the failed-run groups", () => {
    // Server: 1 unseen mention + the approval + 1 failed run = 3.
    expect(needsYouBadge({
      serverCount: 3,
      activityItems: [message("m1"), approval, failedRunActivity],
      attention: { countsBySourceKind: { approval: 1, decision: 2, failed_run: 4 } },
      failedRunGroups: 2,
    })).toBe(1 + 3 + 2);
  });

  it("keeps the server's count when the desk read is missing, and its failed runs when the runs read is", () => {
    expect(needsYouBadge({ serverCount: 5, activityItems: [approval], attention: null, failedRunGroups: 1 })).toBe(5);
    expect(needsYouBadge({
      serverCount: 2, activityItems: [approval, failedRunActivity], attention: { countsBySourceKind: { approval: 1 } }, failedRunGroups: null,
    })).toBe(1 + 1);
  });
});

describe("useNeedsYouBadge", () => {
  it("counts the Inbox's failed-run groups, minus the ones dismissed", () => {
    feeds.current["/attention"] = { countsBySourceKind: {} };
    feeds.current["/inbox/runs"] = { runs: [run("r1", "a1"), run("r2", "a1"), run("r3", "a2")] };
    const { result } = renderHook(() => useNeedsYouBadge());
    expect(result.current).toBe(2);
  });

  it("dismissing an item lowers the rail badge", async () => {
    activity.current = { attentionCount: 2, items: [approval] };
    feeds.current["/attention"] = { countsBySourceKind: { approval: 1, decision: 1 } };
    feeds.current["/attention?includeDismissed=true&all=true"] = {
      generatedAt: NOW, items: [deskItem("a"), deskItem("b", { sourceKind: "decision" })],
    };
    function Page() {
      const badge = useNeedsYouBadge();
      return <><span data-testid="badge">{badge}</span><AttentionDesk /></>;
    }
    const { rerender } = render(<Page />);
    // 2 from the server - the Activity approval + 2 on the desk.
    expect(screen.getByTestId("badge")).toHaveTextContent("3");

    const row = screen.getByText("Title a").closest("[data-attention-row]") as HTMLElement;
    await userEvent.click(within(row).getByRole("button", { name: "Row actions" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Dismiss" }));
    expect(post).toHaveBeenCalledWith("/inbox-dismissals", { itemKey: "attention:a" });
    expect(mutate).toHaveBeenCalledWith(isAttentionKey);

    // The re-read: upstream leaves the dismissed item out of the badge's /attention counts.
    feeds.current["/attention"] = { countsBySourceKind: { decision: 1 } };
    rerender(<Page />);
    expect(screen.getByTestId("badge")).toHaveTextContent("2");
  });
});
