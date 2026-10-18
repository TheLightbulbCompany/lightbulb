import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, renderHook, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AttentionDesk } from "@/components/teams/inbox/AttentionDesk";
import { isAttentionKey, useAttentionFeed } from "@/components/teams/inbox/hooks/useAttentionFeed";
import type { AttentionFeedResponse, AttentionItem } from "@/components/teams/shared/types";

const { readSpy, feeds, post, del, mutate, notifySuccess } = vi.hoisted(() => ({
  readSpy: vi.fn(),
  feeds: { current: {} as Record<string, unknown> },
  post: vi.fn(),
  del: vi.fn(),
  mutate: vi.fn(),
  notifySuccess: vi.fn(),
}));

vi.mock("@/hooks/useTeamsApi", () => ({
  useTeamsApi: () => ({
    read: (path: string | null) => {
      readSpy(path);
      return { data: path ? feeds.current[path] : undefined, isLoading: false, error: undefined, mutate: vi.fn() };
    },
    post,
    del,
    mutate,
  }),
}));
vi.mock("@/lib/notify", () => ({ notifySuccess, notifyApiError: vi.fn() }));
vi.mock("@/components/teams/inbox/DecisionResolver", () => ({
  DecisionResolver: ({ decisionId }: { decisionId: string }) => <div>Decision card {decisionId}</div>,
}));

const DESK = "/attention?includeDismissed=true&all=true";
const NOW = Date.parse("2026-10-18T15:00:00.000Z");
const iso = (ms: number) => new Date(ms).toISOString();
const DAY = 86_400_000;

function item(id: string, over: Partial<AttentionItem> = {}): AttentionItem {
  return {
    id,
    sourceKind: "blocker_attention",
    subject: { kind: "issue", id: `iss-${id}`, title: `Title ${id}`, identifier: null, status: null, href: null },
    whyNow: `why ${id}`,
    severity: "medium",
    activityAt: iso(NOW - 1000),
    createdAt: iso(NOW - 1000),
    relatedIssue: null,
    originAgentName: null,
    dismissalKey: `attention:${id}`,
    dismissal: null,
    decideBy: null,
    rank: 0,
    shelf: false,
    decisionVerbs: [],
    inlineResolvable: false,
    detail: null,
    ...over,
  };
}

function setDesk(items: AttentionItem[], generatedAt = iso(NOW)) {
  feeds.current = { ...feeds.current, [DESK]: { generatedAt, items } satisfies AttentionFeedResponse };
}

const shelfNames = () => screen.getAllByRole("region").map((el) => el.getAttribute("aria-label"));

async function openMenu(row: HTMLElement) {
  await userEvent.click(within(row).getByRole("button", { name: "Row actions" }));
}

const rowOf = (title: string) => screen.getByText(title).closest("[data-attention-row]") as HTMLElement;

beforeEach(() => {
  vi.clearAllMocks();
  feeds.current = {};
  post.mockResolvedValue({});
  del.mockResolvedValue(undefined);
});

describe("AttentionDesk", () => {
  it("shelves Decide now / New today / Earlier", () => {
    setDesk([
      item("old", { createdAt: iso(NOW - 3 * DAY) }),
      item("due", { createdAt: iso(NOW - 5 * DAY), decideBy: "today" }),
      item("new", { createdAt: iso(NOW - 60_000) }),
    ]);
    render(<AttentionDesk />);
    expect(readSpy).toHaveBeenCalledWith(DESK);
    expect(shelfNames()).toEqual(["Decide now", "New today", "Earlier"]);
    expect(within(screen.getByRole("region", { name: "Decide now" })).getByText("Title due")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "New today" })).getByText("Title new")).toBeInTheDocument();
    expect(within(screen.getByRole("region", { name: "Earlier" })).getByText("Title old")).toBeInTheDocument();
  });

  it("omits Decide now when nothing carries a due decide-by", () => {
    setDesk([item("new"), item("later", { decideBy: "whenever" })]);
    render(<AttentionDesk />);
    expect(shelfNames()).toEqual(["New today"]);
  });

  it("row shows eyebrow, whyNow and inline verbs or Open", async () => {
    const issue = { kind: "issue", id: "iss-9", title: "Ship it", identifier: "PAP-9", status: null, href: "/PAP/issues/PAP-9" };
    setDesk([
      item("appr", {
        sourceKind: "approval",
        subject: { kind: "approval", id: "ap-1", title: "Hire a designer", identifier: null, status: null, href: "/PAP/approvals/ap-1" },
        relatedIssue: issue,
        decideBy: "today",
        decideByAttribution: { type: "agent", agentId: "a1", agentName: "Planner", userId: null, runId: null, responsibleUserId: null, updatedAt: iso(NOW) },
        inlineResolvable: true,
        decisionVerbs: [
          { id: "approve", label: "Approve", description: null },
          { id: "reject", label: "Reject", description: null },
        ],
      }),
      item("blk", { subject: { ...issue, title: "Waiting on infra" }, whyNow: "Blocked by PAP-2" }),
    ]);
    render(<AttentionDesk />);

    const approval = rowOf("Hire a designer");
    // Eyebrow: kind, the task key (a shell link), decide-by and who set it.
    expect(within(approval).getByText("Approval")).toBeInTheDocument();
    expect(within(approval).getByRole("link", { name: "PAP-9" })).toHaveAttribute("href", expect.stringContaining("iss-9"));
    expect(within(approval).getByText(/set by Planner/)).toBeInTheDocument();
    expect(within(approval).getByText("why appr")).toBeInTheDocument();

    // The topmost inline decision opens on its own; See less shows the compact verbs.
    expect(within(approval).getByRole("textbox", { name: "Decision note" })).toBeInTheDocument();
    await userEvent.click(within(approval).getAllByRole("button", { name: "Collapse decision" })[0]);
    const verbs = within(approval).getByLabelText("Decision actions");
    // Affirmative verb rightmost.
    expect(within(verbs).getAllByRole("button").map((b) => b.textContent)).toEqual(["Reject", "Approve"]);
    await userEvent.click(within(verbs).getByRole("button", { name: "Approve" }));
    expect(post).toHaveBeenCalledWith("/approvals/ap-1/approve", {});
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(isAttentionKey));
    expect(notifySuccess).toHaveBeenCalledWith("Approval approved");

    // A non-inline row offers Open, to its in-shell page.
    const blocked = rowOf("Waiting on infra");
    expect(within(blocked).getByText("Blocked dependency")).toBeInTheDocument();
    expect(within(blocked).getByText("Blocked by PAP-2")).toBeInTheDocument();
    expect(within(blocked).getByRole("link", { name: /Open/ })).toHaveAttribute("href", expect.stringContaining("PAP-9"));
    expect(within(blocked).queryByLabelText("Decision actions")).not.toBeInTheDocument();
  });

  it("an approval's Reject needs a note, because the BFF requires a reason", async () => {
    setDesk([item("appr", {
      sourceKind: "approval",
      subject: { kind: "approval", id: "ap-1", title: "Hire", identifier: null, status: null, href: null },
      inlineResolvable: true,
      decisionVerbs: [{ id: "reject", label: "Reject", description: null }],
    })]);
    render(<AttentionDesk />);
    const row = rowOf("Hire");
    const reject = within(row).getByRole("button", { name: "Reject" });
    expect(reject).toBeDisabled();
    await userEvent.type(within(row).getByRole("textbox", { name: "Decision note" }), "Over budget");
    await userEvent.click(reject);
    expect(post).toHaveBeenCalledWith("/approvals/ap-1/reject", { reason: "Over budget" });
  });

  it("a collapsed interaction row says whose decision it is above its verbs", async () => {
    setDesk([item("ask", {
      sourceKind: "issue_thread_interaction",
      subject: { kind: "issue", id: "iss-1", title: "Release v2", identifier: null, status: null, href: null },
      inlineResolvable: true,
      decisionVerbs: [{ id: "accept", label: "Accept", description: null }],
      interaction: {
        id: "int-1", companyId: "co", issueId: "iss-1", kind: "request_confirmation", status: "pending",
        continuationPolicy: "none", createdAt: iso(NOW), updatedAt: iso(NOW), payload: { version: 1, prompt: "Ship it?" },
      },
      resolverAudience: {
        requestedResolverPolicy: "anyone", effectiveResolverPolicy: "anyone",
        effectiveResolverPolicySource: "requested", resolverPolicyProvenance: "inherited",
        addresseeAgentId: "a1", addresseeName: "Ada", createdByAgentId: null, createdByAgentName: null,
      },
    })]);
    render(<AttentionDesk />);
    const row = rowOf("Release v2");
    await userEvent.click(within(row).getAllByRole("button", { name: "Collapse decision" })[0]);
    const line = within(row).getByTestId("interaction-audience");
    expect(line).toHaveAttribute("data-audience-variant", "compact");
    expect(line).toHaveTextContent("Only Ada or the board can respond");
    expect(line).toHaveAttribute("title", "Only Ada or a person on the board can respond.");
  });

  it("snooze then Snoozed section lists it with Restore", async () => {
    setDesk([item("a")]);
    const { rerender } = render(<AttentionDesk />);
    await openMenu(rowOf("Title a"));
    const snooze = await screen.findByRole("menuitem", { name: "Snooze" });
    await userEvent.click(snooze);
    await userEvent.keyboard("{ArrowRight}");
    await userEvent.click(await screen.findByRole("menuitem", { name: "1 hour" }));
    expect(post).toHaveBeenCalledWith("/inbox-dismissals", {
      itemKey: "attention:a",
      kind: "snooze",
      snoozedUntil: expect.any(String),
    });
    // Hidden at once, before the server answers.
    expect(screen.queryByText("Title a")).not.toBeInTheDocument();
    await waitFor(() => expect(mutate).toHaveBeenCalledWith(isAttentionKey));

    const until = iso(Date.now() + 3 * 3_600_000);
    setDesk([item("a", { dismissal: { kind: "snooze", dismissedAt: iso(NOW), snoozedUntil: until, isActive: true } })], iso(NOW + 1));
    rerender(<AttentionDesk />);
    await userEvent.click(screen.getByRole("button", { name: "Snoozed (1)" }));
    const row = rowOf("Title a");
    expect(within(row).getByText(/Reappears in 3h/)).toBeInTheDocument();
    await userEvent.click(within(row).getByRole("button", { name: "Restore" }));
    // The key holds ':'; it is encoded into the path.
    expect(del).toHaveBeenCalledWith("/inbox-dismissals/attention%3Aa");
  });

  it("dismiss shows an undo toast and moves it to Dismissed", async () => {
    setDesk([item("a")]);
    const { rerender } = render(<AttentionDesk />);
    await openMenu(rowOf("Title a"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Dismiss" }));
    expect(post).toHaveBeenCalledWith("/inbox-dismissals", { itemKey: "attention:a" });
    expect(screen.queryByText("Title a")).not.toBeInTheDocument();
    expect(notifySuccess).toHaveBeenCalledWith("Dismissed", expect.objectContaining({
      description: "Title a",
      durationMs: 8000,
      action: expect.objectContaining({ label: "Undo" }),
    }));

    setDesk([item("a", { dismissal: { kind: "dismiss", dismissedAt: iso(NOW), snoozedUntil: null, isActive: true } })], iso(NOW + 1));
    rerender(<AttentionDesk />);
    expect(screen.queryByText("Title a")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismissed (1)" }));
    expect(within(rowOf("Title a")).getByRole("button", { name: "Restore" })).toBeInTheDocument();

    // Undo restores through the same encoded DELETE.
    const { action } = notifySuccess.mock.calls[0][1] as { action: { onClick: () => void } };
    act(() => action.onClick());
    expect(del).toHaveBeenCalledWith("/inbox-dismissals/attention%3Aa");
  });

  it("an Undo pressed after a newer feed landed keeps the pending state made on that feed", async () => {
    setDesk([item("a"), item("b")]);
    const { rerender } = render(<AttentionDesk />);
    await openMenu(rowOf("Title a"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Dismiss" }));
    const { action } = notifySuccess.mock.calls[0][1] as { action: { onClick: () => void } };

    // A newer feed lands (a is dismissed on the server), then b is dismissed on it.
    setDesk([item("a", { dismissal: { kind: "dismiss", dismissedAt: iso(NOW), snoozedUntil: null, isActive: true } }), item("b")], iso(NOW + 1));
    rerender(<AttentionDesk />);
    await openMenu(rowOf("Title b"));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Dismiss" }));
    expect(screen.queryByText("Title b")).not.toBeInTheDocument();

    // The first toast's Undo, from the older render: b stays hidden.
    act(() => action.onClick());
    expect(screen.queryByText("Title b")).not.toBeInTheDocument();
    expect(del).toHaveBeenCalledWith("/inbox-dismissals/attention%3Aa");
  });

  it("leaves its failed runs out when For you lists the failed-run groups", () => {
    setDesk([item("run", { sourceKind: "failed_run" }), item("blk")]);
    const { unmount } = render(<AttentionDesk hideFailedRuns />);
    expect(screen.queryByText("Title run")).not.toBeInTheDocument();
    expect(screen.getByText("Title blk")).toBeInTheDocument();
    unmount();
    render(<AttentionDesk />);
    expect(screen.getByText("Title run")).toBeInTheDocument();
  });

  it("idle N days after 30 days", async () => {
    setDesk([item("stale", { shelf: true, activityAt: iso(NOW - 45 * DAY), createdAt: iso(NOW - 60 * DAY) })]);
    render(<AttentionDesk />);
    // Off the desk shelves, onto Aging.
    expect(screen.queryByRole("region", { name: "Earlier" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Aging (1)" }));
    expect(screen.getByText("Idle 45 days")).toBeInTheDocument();
    expect(screen.getByText("Title stale")).toBeInTheDocument();
  });

  it("badge count ignores dismissed and snoozed items", () => {
    // The desk's read includes the hidden rows; the badge reads its own key, which does not.
    setDesk([
      item("live", { sourceKind: "approval" }),
      item("gone", { sourceKind: "approval", dismissal: { kind: "dismiss", dismissedAt: iso(NOW), snoozedUntil: null, isActive: true } }),
      item("later", { sourceKind: "approval", dismissal: { kind: "snooze", dismissedAt: iso(NOW), snoozedUntil: iso(NOW + DAY), isActive: true } }),
    ]);
    feeds.current["/attention"] = { items: [item("live", { sourceKind: "approval" })], countsBySourceKind: { approval: 1 } };
    render(<AttentionDesk />);
    const { result } = renderHook(() => useAttentionFeed());
    expect(result.current.count).toBe(1);
    expect(readSpy.mock.calls.filter(([path]) => path === "/attention")).toHaveLength(1);
    // The dismissal writes refresh both keys.
    expect(isAttentionKey("/teams/attention")).toBe(true);
    expect(isAttentionKey(`/teams${DESK}`)).toBe(true);
    expect(isAttentionKey("/teams/attention-x")).toBe(false);
  });

  it("says all caught up when nothing is waiting", () => {
    setDesk([]);
    render(<AttentionDesk />);
    expect(screen.getByText("Nothing needs a decision from you right now.")).toBeInTheDocument();
  });
});
