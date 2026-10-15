import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ActivityItem } from "@lightbulb/shared";
import { ForYou, type ForYouProps } from "../ForYou";
import type { ForYouFilter } from "../activityPage";
import { approval, ask, attention, loginAsk, maya, message, runGroup } from "./fixtures";

const wakeAgent = vi.fn().mockResolvedValue(undefined);
vi.mock("@/components/teams/inbox/hooks/useAgentWake", () => ({ useAgentWake: () => ({ wakeAgent }) }));
vi.mock("@/lib/notify", () => ({ notifyApiError: vi.fn() }));
vi.mock("@/components/teams/inbox/DecisionResolver", () => ({
  DecisionResolver: ({ decisionId }: { decisionId: string }) => <div>Decision card {decisionId}</div>,
}));

/** The page's side of the chips: the filter lives in the URL and comes back as a prop. */
function Harness(props: ForYouProps & { onFilter?: (filter: ForYouFilter) => void }) {
  const [filter, setFilter] = useState<ForYouFilter>(props.filter ?? "all");
  return <ForYou {...props} filter={filter} onFilterChange={(next) => { props.onFilter?.(next); setFilter(next); }} />;
}

function setup(over: Partial<ForYouProps> & { onFilter?: (filter: ForYouFilter) => void } = {}) {
  const props = {
    activityItems: [approval, message("m1")], attentionItems: [], loading: false,
    onRunAction: vi.fn().mockResolvedValue(undefined), onNavigate: vi.fn(), onDismissRuns: vi.fn(), people: [], agents: [], ...over,
  } satisfies ForYouProps;
  render(<Harness {...props} />);
  return props;
}

const rows = () => [...document.querySelectorAll("[data-for-you-id]")].map((row) => row.getAttribute("data-for-you-id"));
const chip = (name: string) => screen.getByRole("button", { name });

/** IntersectionObserver stand-in: every observer created, so a test can scroll the sentinel into view. */
const observers: { callback: IntersectionObserverCallback; target: Element | null; live: boolean }[] = [];
class FakeObserver {
  entry: (typeof observers)[number];
  constructor(callback: IntersectionObserverCallback) { this.entry = { callback, target: null, live: true }; observers.push(this.entry); }
  observe(target: Element) { this.entry.target = target; }
  disconnect() { this.entry.live = false; }
}
afterEach(() => { observers.length = 0; vi.unstubAllGlobals(); });
function scrollToEnd() {
  for (const entry of observers.filter((o) => o.live && o.target)) {
    act(() => entry.callback([{ isIntersecting: true, target: entry.target! } as IntersectionObserverEntry], {} as IntersectionObserver));
  }
}

describe("ForYou", () => {
  it("shows one row per item, newest first: who, where, the message, when, and an unread dot", () => {
    setup({ activityItems: [approval, message("m1"), message("m2"), message("m3", maya, true)], failedRuns: [runGroup("g1")] });
    expect(rows()).toEqual(["mention:m1", "mention:m2", "mention:m3", "paperclip:approval:a1", "failed_run:g1"]);
    const m1 = document.querySelector('[data-for-you-id="mention:m1"]')!;
    expect(m1).toHaveTextContent("Maya");
    expect(m1).toHaveTextContent("in #launch");
    expect(m1).toHaveTextContent("message m1");
    expect(m1.querySelector("time")).toHaveAttribute("dateTime", "2026-09-08T11:00:00Z");
    expect(m1).toHaveAttribute("data-unread", "true");
    expect(document.querySelector('[data-for-you-id="mention:m3"]')).not.toHaveAttribute("data-unread");
    // Two unread messages; the approval carries no dot (an ask has no "opened" state).
    expect(screen.getAllByRole("img", { name: "Unread" })).toHaveLength(2);
    expect(document.querySelector('[data-for-you-id="paperclip:approval:a1"]')).not.toHaveAttribute("data-unread");
    // No verb groups any more.
    expect(screen.queryByRole("heading", { level: 3 })).not.toBeInTheDocument();
  });

  it("filters by chip, tells the page so the URL follows, and keeps the chip a link picked", async () => {
    const onFilter = vi.fn();
    setup({ activityItems: [approval, message("m1"), message("d1", maya, false, "dm"), message("t1", maya, false, "thread_reply")], onFilter });
    expect(chip("All")).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(chip("DMs"));
    expect(onFilter).toHaveBeenLastCalledWith("dms");
    expect(chip("DMs")).toHaveAttribute("aria-pressed", "true");
    expect(rows()).toEqual(["dm:d1"]);
    await userEvent.click(chip("Threads"));
    expect(rows()).toEqual(["thread_reply:t1"]);
    await userEvent.click(chip("Mentions"));
    expect(rows()).toEqual(["mention:m1"]);
    await userEvent.click(chip("Needs you"));
    expect(rows()).toEqual(["paperclip:approval:a1"]);
    // Re-picking the selected chip changes nothing (no history entry).
    onFilter.mockClear();
    await userEvent.click(chip("Needs you"));
    expect(onFilter).not.toHaveBeenCalled();
  });

  it("starts on the chip the link names", () => {
    setup({ activityItems: [approval, message("m1")], filter: "needs-you" });
    expect(chip("Needs you")).toHaveAttribute("aria-pressed", "true");
    expect(rows()).toEqual(["paperclip:approval:a1"]);
  });

  it("Unread hides read messages and keeps open asks", async () => {
    setup({ activityItems: [approval, message("m1"), message("m2", maya, true)] });
    await userEvent.click(chip("Unread"));
    expect(chip("Unread")).toHaveAttribute("aria-pressed", "true");
    expect(rows()).toEqual(["mention:m1", "paperclip:approval:a1"]);
    await userEvent.click(chip("Unread"));
    expect(rows()).toHaveLength(3);
  });

  it("opens a row in context and marks it read", async () => {
    const props = setup({ activityItems: [message("m1")] });
    await userEvent.click(screen.getByRole("button", { name: /Maya in #launch .*message m1/ }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "thread", id: "m1", roomId: "room-1" });
    expect(document.querySelector('[data-for-you-id="mention:m1"]')).not.toHaveAttribute("data-unread");
    expect(screen.queryByRole("img", { name: "Unread" })).not.toBeInTheDocument();
  });

  it("a message row only opens: no inline reply", () => {
    setup({ activityItems: [message("m1")] });
    expect(screen.queryByRole("button", { name: "Reply" })).not.toBeInTheDocument();
  });

  it("loads the next page as the end scrolls into view, with a quiet loading line and no Show older button", () => {
    vi.stubGlobal("IntersectionObserver", FakeObserver);
    const onLoadMore = vi.fn();
    const props = { activityItems: [message("m1")], attentionItems: [], loading: false, onRunAction: vi.fn(), onNavigate: vi.fn(), people: [], agents: [] };
    const view = render(<ForYou {...props} hasMore onLoadMore={onLoadMore} />);
    expect(screen.queryByRole("button", { name: "Show older" })).not.toBeInTheDocument();
    scrollToEnd();
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    view.rerender(<ForYou {...props} hasMore loadingMore onLoadMore={onLoadMore} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading older items…");
    scrollToEnd();
    expect(onLoadMore).toHaveBeenCalledTimes(1);
    view.rerender(<ForYou {...props} hasMore={false} onLoadMore={onLoadMore} />);
    expect(document.querySelector(".for-you__sentinel")).toBeNull();
  });

  it("looks again for the sentinel when Unread changes, so an empty filtered view still pages", async () => {
    vi.stubGlobal("IntersectionObserver", FakeObserver);
    const onLoadMore = vi.fn();
    render(<ForYou activityItems={[message("m3", maya, true)]} attentionItems={[]} loading={false} onRunAction={vi.fn()}
      onNavigate={vi.fn()} people={[]} agents={[]} hasMore onLoadMore={onLoadMore} />);
    const before = observers.length;
    await userEvent.click(screen.getByRole("button", { name: "Unread" }));
    expect(rows()).toEqual([]);
    expect(observers.length).toBeGreaterThan(before);
    expect(observers.filter((o) => o.live)).toHaveLength(1);
    scrollToEnd();
    expect(onLoadMore).toHaveBeenCalledTimes(1);
  });

  it("does not claim all caught up while older pages may still hold items", () => {
    setup({ activityItems: [], hasMore: true, onLoadMore: vi.fn() });
    expect(screen.queryByText("You're all caught up.")).not.toBeInTheDocument();
  });

  it("never approves from the row: Review opens the approval's own detail with its payload", async () => {
    const props = setup({ activityItems: [approval] });
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "approval", id: "a1", roomId: null });
    expect(props.onRunAction).not.toHaveBeenCalled();
  });

  it("reviews a confirmation or a session ask in its own place, and asks once inline when it has none", async () => {
    const confirm = { ...ask, id: "paperclip:interaction:iss-4:int-2", actions: [
      { id: "accept", label: "Confirm", style: "primary" as const, confirm: null, input: null, navigate: null },
      { id: "open", label: "Open task", style: null, confirm: null, input: null, navigate: { kind: "issue" as const, id: "iss-4", roomId: null } },
    ] };
    const join = { ...approval, id: "session:join:s1", kind: "session" as const, subtype: "join_request" as const, createdAt: "2026-09-08T09:00:00Z",
      summary: { ...approval.summary, links: [{ kind: "session" as const, id: "s1", label: "Session" }] },
      actions: [{ id: "allow", label: "Allow", style: "primary" as const, confirm: null, input: null, navigate: null }] };
    const extension = { ...approval, id: "extension:e1", kind: "extension" as const, subtype: "extension" as const, actor: maya, createdAt: "2026-09-08T08:00:00Z",
      summary: { ...approval.summary, links: [] }, actions: [{ id: "add", label: "Add", style: "primary" as const, confirm: null, input: null, navigate: null }] };
    const props = setup({ activityItems: [confirm, join, extension] });
    const [first, second, third] = screen.getAllByRole("button", { name: "Review" });
    await userEvent.click(first);
    expect(props.onNavigate).toHaveBeenLastCalledWith({ kind: "issue", id: "iss-4", roomId: null });
    await userEvent.click(second);
    expect(props.onNavigate).toHaveBeenLastCalledWith({ kind: "session", id: "s1", roomId: null });
    await userEvent.click(third);
    expect(props.onRunAction).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(props.onRunAction).toHaveBeenCalledWith("extension:e1", "add", {});
  });

  it("offers the server's decline beside a login or extension ask, and runs it", async () => {
    const notNow = { id: "decline", label: "Not now", style: null, confirm: null, input: null, navigate: null };
    const extension = { ...approval, id: "extension:e1", kind: "extension" as const, subtype: "extension" as const, createdAt: "2026-09-08T08:00:00Z",
      summary: { ...approval.summary, links: [] }, actions: [{ id: "add", label: "Add", style: "primary" as const, confirm: null, input: null, navigate: null }, notNow] };
    const props = setup({ activityItems: [{ ...loginAsk, actions: [...loginAsk.actions, notNow] }, extension] });
    const [loginDecline, extensionDecline] = screen.getAllByRole("button", { name: "Not now" });
    await userEvent.click(loginDecline);
    expect(props.onRunAction).toHaveBeenCalledWith("login:abc", "decline", {});
    await userEvent.click(extensionDecline);
    expect(props.onRunAction).toHaveBeenCalledWith("extension:e1", "decline", {});
  });

  it("retries and dismisses a failed run like the Inbox did", async () => {
    const props = setup({ activityItems: [], failedRuns: [runGroup("g1", "friday", ["r1", "r2"])], agents: [{ id: "friday", name: "Friday" } as never] });
    expect(screen.getByText("Timed out ×2")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(wakeAgent).toHaveBeenCalledWith("friday", { reason: "retry" });
    expect(await screen.findByRole("button", { name: "Retried" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(props.onDismissRuns).toHaveBeenCalledWith(["r1", "r2"]);
    await userEvent.click(screen.getByRole("button", { name: /Friday in Work .*Timed out/ }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "run", id: "r1", roomId: null });
  });

  it("says so when everything is done", () => {
    setup({ activityItems: [{ ...message("m1"), state: "done" }, { ...approval, state: "resolved" } as ActivityItem] });
    expect(screen.getByText("You're all caught up.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "For you" })).toBeInTheDocument();
  });

  it("never shows the all-clear while a source is loading or failed, and offers Retry", async () => {
    const onRetry = vi.fn();
    setup({ activityItems: [], incomplete: true, onRetry });
    expect(screen.queryByText("You're all caught up.")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load some items · Retry");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });

  it("says Loading, not all caught up, while any source is still answering", () => {
    setup({ activityItems: [], loading: true });
    expect(screen.getByRole("status")).toHaveTextContent("Loading…");
    expect(screen.queryByText("You're all caught up.")).not.toBeInTheDocument();
  });

  it("keeps rows from the sources that answered beside the failure line", () => {
    setup({ activityItems: [message("m1")], incomplete: true });
    expect(screen.getByText("message m1")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't load some items");
  });

  it("reviews an approval from the attention feed on its own page when Activity could not list it", async () => {
    const props = setup({ activityItems: [], attentionItems: [{ ...attention("x3", "approval"), subject: { ...attention("x3", "approval").subject, id: "ap-7" } }] });
    await userEvent.click(screen.getByRole("button", { name: "Review" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "approval", id: "ap-7", roomId: null });
  });

  it("answers an agent's decision with the existing decision card, and opens a blocked task", async () => {
    const props = setup({ activityItems: [], attentionItems: [attention("d1", "decision"), attention("b1", "blocker_attention", "Ops")] });
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    expect(screen.getByText("Decision card subject-d1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "issue", id: "issue-b1", roomId: null });
  });

  it("takes a login an agent asked for in a masked form and posts it as the value only", async () => {
    const props = setup({ activityItems: [loginAsk] });
    await userEvent.click(screen.getByRole("button", { name: "Add login" }));
    expect(screen.getByText("Saved to the workspace's website logins.")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("textbox", { name: "Username or email" }), "me@x.com");
    await userEvent.type(screen.getByLabelText("Password"), "pw");
    await userEvent.click(screen.getAllByRole("button", { name: "Add login" }).at(-1)!);
    expect(props.onRunAction).toHaveBeenCalledWith("login:abc", "provide", { value: JSON.stringify({ username: "me@x.com", password: "pw" }) });
  });

  it("keeps the item and says why when its action fails", async () => {
    setup({ activityItems: [ask], onRunAction: vi.fn().mockRejectedValue(new Error("Already decided")) });
    await userEvent.click(screen.getByRole("button", { name: "Answer" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Your answer" }), "o1");
    await userEvent.click(screen.getAllByRole("button", { name: "Answer" }).at(-1)!);
    expect(await screen.findByRole("alert")).toHaveTextContent("Already decided");
  });

  it("highlights the item a notification points at", () => {
    setup({ activityItems: [message("m1"), message("m2")], focusId: "mention:m2" });
    expect(document.querySelector('[data-for-you-id="mention:m2"]')).toHaveAttribute("data-highlighted", "true");
  });
});
