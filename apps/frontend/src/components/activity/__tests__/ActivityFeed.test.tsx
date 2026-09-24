import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ActivityItem } from "@lightbulb/shared";
import { ActivityFeed, type ActivityFeedProps } from "../ActivityFeed";

const approval: ActivityItem = {
  id: "paperclip:approval:a1", kind: "paperclip", subtype: "approval", roomId: null, messageId: null, threadRootMessageId: null,
  actor: { kind: "agent", id: "growth", name: "Growth" }, createdAt: new Date(Date.now() - 60_000).toISOString(),
  state: "open", resolvedBy: null, resolvedAt: null, read: false,
  summary: {
    title: "Growth wants to publish a post",
    requester: { kind: "agent", id: "growth", name: "Growth" },
    reason: "Launch plan, step 3",
    preview: { text: "We just shipped usage-based pricing.", destination: "Company X account", fields: [{ label: "Audience", value: "Public" }] },
    links: [{ kind: "issue", id: "issue-41", label: "LB-41" }],
  },
  actions: [
    { id: "approve", label: "Approve", style: "primary", confirm: null, input: null, navigate: null },
    { id: "reject", label: "Reject", style: "destructive", confirm: "Reject this post?", input: { kind: "text", label: "Why? (optional)", required: false, options: null }, navigate: null },
    { id: "open", label: "Open task", style: null, confirm: null, input: null, navigate: { kind: "issue", id: "issue-41", roomId: null } },
  ],
};
const mention: ActivityItem = {
  ...approval, id: "mention:m1", kind: "mention", subtype: null, roomId: "room-1", messageId: "m1", read: true,
  summary: { title: "Maya mentioned you in #launch", requester: { kind: "human", id: "user_maya", name: "Maya" }, reason: null,
    preview: { text: "@you can you check the copy?", destination: null, fields: [] }, links: [] },
  actions: [
    { id: "reply", label: "Reply", style: "primary", confirm: null, input: { kind: "text", label: "Reply", required: true, options: null }, navigate: null },
    { id: "reassign", label: "Reassign", style: null, confirm: null, input: { kind: "person", label: "Choose someone", required: true, options: null }, navigate: null },
  ],
};

function setup(overrides: Partial<ActivityFeedProps> = {}) {
  const props: ActivityFeedProps = {
    items: [approval, mention], loading: false, error: false, paperclipAvailable: true, hasMore: false,
    onLoadMore: vi.fn(), onRunAction: vi.fn().mockResolvedValue(undefined), onNavigate: vi.fn(),
    people: [{ value: "user_jon", label: "Jon" }], onOpenCompanyActivity: vi.fn(), ...overrides,
  };
  render(<ActivityFeed {...props} />);
  return props;
}

describe("ActivityFeed", () => {
  it("renders the server's summary: what, who, why, the payload and where it goes", () => {
    setup();
    expect(screen.getByText("Growth wants to publish a post")).toBeInTheDocument();
    expect(screen.getByText("Launch plan, step 3")).toBeInTheDocument();
    expect(screen.getByText("To: Company X account")).toBeInTheDocument();
    expect(screen.getByText("We just shipped usage-based pricing.")).toBeInTheDocument();
    expect(screen.getByText("Public")).toBeInTheDocument();
    expect(screen.queryByText(/distribution\./)).not.toBeInTheDocument();
  });

  it("runs a plain action directly and navigates without posting", async () => {
    const props = setup();
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(props.onRunAction).toHaveBeenCalledWith(approval.id, "approve", {});
    await userEvent.click(screen.getByRole("button", { name: "Open task" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "issue", id: "issue-41", roomId: null });
    await userEvent.click(screen.getByRole("button", { name: "LB-41" }));
    expect(props.onRunAction).toHaveBeenCalledTimes(1);
  });

  it("asks before a confirm action and sends its optional note", async () => {
    const props = setup();
    await userEvent.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByText("Reject this post?")).toBeInTheDocument();
    expect(props.onRunAction).not.toHaveBeenCalled();
    await userEvent.type(screen.getByRole("textbox", { name: "Why? (optional)" }), "Off brand");
    const submits = screen.getAllByRole("button", { name: "Reject" });
    await userEvent.click(submits[submits.length - 1]);
    expect(props.onRunAction).toHaveBeenCalledWith(approval.id, "reject", { note: "Off brand", value: "Off brand" });
  });

  it("replies inline and requires the text, and picks a person from the roster", async () => {
    const props = setup();
    await userEvent.click(screen.getByRole("button", { name: "Reply" }));
    const send = screen.getAllByRole("button", { name: "Reply" }).at(-1)!;
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByRole("textbox", { name: "Reply" }), "On it");
    await userEvent.click(send);
    expect(props.onRunAction).toHaveBeenCalledWith(mention.id, "reply", { note: "On it", value: "On it" });

    await userEvent.click(screen.getByRole("button", { name: "Reassign" }));
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Choose someone" }), "user_jon");
    await userEvent.click(screen.getAllByRole("button", { name: "Reassign" }).at(-1)!);
    expect(props.onRunAction).toHaveBeenCalledWith(mention.id, "reassign", { note: "user_jon", value: "user_jon" });
  });

  it("keeps the item and says so when an action fails", async () => {
    setup({ onRunAction: vi.fn().mockRejectedValue(new Error("Approval already resolved")) });
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Approval already resolved");
    expect(screen.getByText("Growth wants to publish a post")).toBeInTheDocument();
  });

  it("shows who resolved an item and offers no actions on it", () => {
    setup({ items: [{ ...approval, state: "resolved", resolvedBy: { kind: "human", id: "user_jon", name: "Jon" },
      resolvedAt: new Date().toISOString(), actions: [] }] });
    expect(screen.getByText(/Resolved by Jon/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("says when company items are unavailable and links to company activity", async () => {
    const props = setup({ paperclipAvailable: false });
    expect(screen.getByText(/couldn.t load right now/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Company activity" }));
    expect(props.onOpenCompanyActivity).toHaveBeenCalled();
  });
});
