import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActivityItem } from "@lightbulb/shared";
import { ActivityFeed, type ActivityFeedProps } from "../ActivityFeed";

const growth = { kind: "agent" as const, id: "growth", name: "Growth" };
const approval: ActivityItem = {
  id: "paperclip:approval:a1", kind: "paperclip", subtype: "approval", roomId: null, messageId: null, threadRootMessageId: null,
  actor: growth, createdAt: "2026-09-08T10:00:00Z",
  state: "open", resolvedBy: null, resolvedAt: null, read: false,
  summary: {
    title: "Growth wants to publish a post",
    requester: growth,
    reason: "Launch plan, step 3",
    preview: { text: "We just shipped usage-based pricing.", destination: "Company X account", fields: [{ label: "Recommended action", value: "Post it" }] },
    links: [{ kind: "issue", id: "issue-41", label: "LB-41" }],
  },
  actions: [
    { id: "approve", label: "Approve", style: "primary", confirm: null, input: null, navigate: null },
    { id: "reject", label: "Reject", style: "destructive", confirm: "Reject this post?", input: { kind: "text", label: "Why? (optional)", required: false, options: null }, navigate: null },
    { id: "open", label: "Open task", style: null, confirm: null, input: null, navigate: { kind: "issue", id: "issue-41", roomId: null } },
  ],
};
const maya = { kind: "human" as const, id: "user_maya", name: "Maya" };
const mention: ActivityItem = {
  ...approval, id: "mention:m1", kind: "mention", subtype: null, roomId: "room-1", messageId: "m1", read: true,
  actor: maya, createdAt: new Date(Date.now() - 60_000).toISOString(),
  summary: { title: "Maya mentioned you in #launch", requester: maya, reason: null,
    preview: { text: "@you can you check the copy?", destination: null, fields: [] }, links: [{ kind: "room", id: "room-1", label: "#launch" }] },
  actions: [
    { id: "reply", label: "Reply", style: "primary", confirm: null, input: { kind: "text", label: "Reply", required: true, options: null }, navigate: null },
    { id: "reassign", label: "Reassign", style: null, confirm: null, input: { kind: "person", label: "Choose someone", required: true, options: null }, navigate: null },
    { id: "open", label: "Open thread", style: null, confirm: null, input: null, navigate: { kind: "thread", id: "m1", roomId: "room-1" } },
  ],
};
const thread: ActivityItem = {
  ...mention, id: "thread_reply:t1", kind: "thread_reply", createdAt: "2026-09-01T09:00:00Z", read: false,
  summary: { ...mention.summary, title: "Maya replied in a thread in #launch", preview: { text: "done", destination: null, fields: [] } },
  actions: [],
};

function setup(overrides: Partial<ActivityFeedProps> = {}) {
  const props: ActivityFeedProps = {
    items: [approval, mention], loading: false, error: false, paperclipAvailable: true, hasMore: false,
    onLoadMore: vi.fn(), onRunAction: vi.fn().mockResolvedValue(undefined), onNavigate: vi.fn(),
    people: [{ value: "user_jon", label: "Jon" }], onOpenCompanyActivity: vi.fn(), ...overrides,
  };
  const view = render(<ActivityFeed {...props} />);
  return { props, ...view };
}

const row = (id: string) => document.querySelector<HTMLElement>(`[data-activity-id="${id}"]`)!;

describe("ActivityFeed", () => {
  it("pins Needs you above newer activity and reads like Slack: bold actor, verb, snippet, where", () => {
    setup();
    const headings = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(["Needs you1", "Today"]);
    const pinned = row(approval.id);
    expect(within(pinned).getByText("Growth").tagName).toBe("STRONG");
    expect(within(pinned).getByText("wants to publish a post")).toBeInTheDocument();
    expect(within(pinned).getByText("Launch plan, step 3")).toBeInTheDocument();
    expect(within(pinned).getByText("To Company X account")).toBeInTheDocument();
    expect(within(pinned).getByText("We just shipped usage-based pricing.")).toBeInTheDocument();
    expect(within(pinned).getByText("Recommended action")).toBeInTheDocument();
    expect(pinned).toHaveAttribute("data-unread", "true");
    expect(row(mention.id)).not.toHaveAttribute("data-unread");
    expect(screen.queryByText(/distribution\./)).not.toBeInTheDocument();
  });

  it("decides inline, opens, and follows the location link", async () => {
    const { props } = setup();
    const pinned = row(approval.id);
    await userEvent.click(within(pinned).getByRole("button", { name: "Approve" }));
    expect(props.onRunAction).toHaveBeenCalledWith(approval.id, "approve", {});
    await userEvent.click(within(pinned).getByRole("button", { name: "Open task" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "issue", id: "issue-41", roomId: null });
    await userEvent.click(within(pinned).getByRole("button", { name: "LB-41" }));
    expect(props.onNavigate).toHaveBeenLastCalledWith({ kind: "issue", id: "issue-41", roomId: null });
    expect(props.onRunAction).toHaveBeenCalledTimes(1);
  });

  it("asks before a confirm action and sends its optional note", async () => {
    const { props } = setup();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByText("Reject this post?")).toBeInTheDocument();
    expect(props.onRunAction).not.toHaveBeenCalled();
    await userEvent.type(screen.getByRole("textbox", { name: "Why? (optional)" }), "Off brand");
    const submits = screen.getAllByRole("button", { name: "Reject" });
    await userEvent.click(submits[submits.length - 1]);
    expect(props.onRunAction).toHaveBeenCalledWith(approval.id, "reject", { note: "Off brand", value: "Off brand" });
  });

  it("offers quick actions on an ordinary row: reply inline, pick a person", async () => {
    const { props } = setup();
    const quick = within(row(mention.id)).getByRole("group", { name: "Quick actions" });
    await userEvent.click(within(quick).getByRole("button", { name: "Reply" }));
    const form = () => screen.getByRole("textbox", { name: "Reply" }).closest("form")!;
    const send = within(form()).getByRole("button", { name: "Reply" });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Reply" }), "On it");
    await userEvent.click(send);
    expect(props.onRunAction).toHaveBeenCalledWith(mention.id, "reply", { note: "On it", value: "On it" });

    await userEvent.click(within(quick).getByRole("button", { name: "Reassign" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Choose someone" }), "user_jon");
    const picker = screen.getByRole("combobox", { name: "Choose someone" }).closest("form")!;
    await userEvent.click(within(picker).getByRole("button", { name: "Reassign" }));
    expect(props.onRunAction).toHaveBeenCalledWith(mention.id, "reassign", { note: "user_jon", value: "user_jon" });
  });

  it("filters with chips and says something useful when a filter is empty", async () => {
    setup({ items: [approval, mention, thread] });
    await userEvent.click(screen.getByRole("button", { name: "Threads" }));
    expect(screen.getByRole("button", { name: "Threads" })).toHaveAttribute("aria-pressed", "true");
    expect(row(thread.id)).toBeInTheDocument();
    expect(document.querySelector(`[data-activity-id="${approval.id}"]`)).toBeNull();
    expect(screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent)).toEqual(["Earlier"]);
    await userEvent.click(screen.getByRole("button", { name: "Agents’ work" }));
    expect(screen.getByText(/No agent work to show/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Needs you/ }));
    expect(row(approval.id)).toBeInTheDocument();
  });

  it("opens on a deep link's filter and highlights the focused item", () => {
    setup({ items: [approval, mention], initialFilter: "mentions", focusId: approval.id });
    // The focused approval is not a mention, so the feed widens to All to show it.
    expect(screen.getByRole("button", { name: "All" })).toHaveAttribute("aria-pressed", "true");
    expect(row(approval.id)).toHaveAttribute("data-highlighted", "true");
  });

  it("moves with j / k and opens with Enter", async () => {
    const { props } = setup();
    row(approval.id).focus();
    await userEvent.keyboard("j");
    expect(row(mention.id)).toHaveFocus();
    await userEvent.keyboard("{Enter}");
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "thread", id: "m1", roomId: "room-1" });
    await userEvent.keyboard("k");
    expect(row(approval.id)).toHaveFocus();
  });

  it("keeps the item and says so when an action fails", async () => {
    setup({ onRunAction: vi.fn().mockRejectedValue(new Error("Approval already resolved")) });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Approval already resolved");
    expect(row(approval.id)).toBeInTheDocument();
  });

  it("shows who resolved an item and offers no actions on it", () => {
    setup({ items: [{ ...approval, state: "resolved", read: true, resolvedBy: { kind: "human", id: "user_jon", name: "Jon" },
      resolvedAt: new Date().toISOString(), actions: [] }] });
    expect(screen.getByText(/Resolved by Jon/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("says when company items are unavailable and links to company activity", async () => {
    const { props } = setup({ paperclipAvailable: false });
    expect(screen.getByText(/couldn.t load right now/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Company activity" }));
    expect(props.onOpenCompanyActivity).toHaveBeenCalled();
  });
});
