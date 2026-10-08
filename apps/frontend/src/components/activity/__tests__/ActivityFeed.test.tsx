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

const ask: ActivityItem = {
  ...approval, id: "paperclip:interaction:iss-4:int-1", subtype: "interaction",
  summary: {
    title: "Growth has a question for you: Pricing page", requester: growth, reason: "Which plan name?",
    preview: null, links: [{ kind: "issue", id: "iss-4", label: "LB-4" }],
  },
  actions: [
    { id: "answer", label: "Answer", style: "primary", confirm: null, navigate: null,
      input: { kind: "select", label: "Your answer", required: true, options: [{ value: "o1", label: "Pro" }, { value: "o2", label: "Team" }] } },
    { id: "open", label: "Open task", style: null, confirm: null, input: null, navigate: { kind: "issue", id: "iss-4", roomId: null } },
  ],
};

const friday = { kind: "agent" as const, id: "friday", name: "Friday" };
const loginAsk: ActivityItem = {
  ...approval, id: "login:abc", kind: "login", subtype: "login", actor: friday, roomId: "room-1",
  summary: { title: "Friday needs the login for supademo.com", requester: friday, reason: "Saved to the workspace's website logins.",
    preview: null, links: [{ kind: "room", id: "room-1", label: "Room" }] },
  actions: [
    { id: "provide", label: "Add login", style: "primary", confirm: null, navigate: null,
      input: { kind: "login", label: "Login for supademo.com", required: true, options: null } },
    { id: "decline", label: "Not now", style: null, confirm: null, input: null, navigate: null },
  ],
};

describe("ActivityFeed", () => {
  it("pins an agent's question under Needs you and answers it inline", async () => {
    const { props } = setup({ items: [ask, mention] });
    const pinned = row(ask.id);
    expect(pinned.closest("section")).toHaveAttribute("data-section", "needs-you");
    expect(within(pinned).getByText("has a question for you: Pricing page")).toBeInTheDocument();
    expect(within(pinned).getByText("Which plan name?")).toBeInTheDocument();
    await userEvent.click(within(pinned).getByRole("button", { name: "Answer" }));
    await userEvent.selectOptions(within(pinned).getByRole("combobox", { name: "Your answer" }), "o2");
    const submits = within(pinned).getAllByRole("button", { name: "Answer" });
    await userEvent.click(submits[submits.length - 1]);
    expect(props.onRunAction).toHaveBeenCalledWith(ask.id, "answer", { note: "o2", value: "o2" });
  });

  it("opens a mention's thread from its Open thread action", async () => {
    const { props } = setup();
    await userEvent.click(within(row(mention.id)).getByRole("button", { name: "Open thread" }));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "thread", id: "m1", roomId: "room-1" });
  });

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

  it("pins a teammate's ask to join a session with Allow and Decline inline", async () => {
    const bob = { kind: "human" as const, id: "user_bob", name: "Bob" };
    const join: ActivityItem = {
      ...approval, id: "join:s1:user_bob:r1", kind: "session", subtype: "join_request", actor: bob,
      summary: { title: "Bob wants to join Fix login", requester: bob, reason: null, preview: null, links: [{ kind: "session", id: "s1", label: "Session" }] },
      actions: [
        { id: "allow", label: "Allow", style: "primary", confirm: null, input: null, navigate: null },
        { id: "decline", label: "Decline", style: null, confirm: null, input: null, navigate: null },
      ],
    };
    const { props } = setup({ items: [join] });
    const pinned = row(join.id);
    expect(pinned).toHaveAttribute("data-pinned", "true");
    await userEvent.click(within(pinned).getByRole("button", { name: "Allow" }));
    expect(props.onRunAction).toHaveBeenCalledWith(join.id, "allow", {});
    expect(within(pinned).getByRole("button", { name: "Decline" })).toBeInTheDocument();
  });

  it("answers an agent's ask to message a session, opens the session, and shows the answer", async () => {
    const reason = "Nothing is sent until you choose. Change this in Settings → Desktop.";
    const agentAsk: ActivityItem = {
      ...approval, id: "agentmsg:s1:req-1", kind: "session", subtype: "join_request", actor: friday,
      summary: { title: "Friday wants to message Fix login", requester: friday, reason,
        preview: { text: "Run the tests again", destination: null, fields: [] },
        links: [{ kind: "session", id: "s1", label: "Session" }, { kind: "room", id: "room-1", label: "Room" }] },
      actions: [
        { id: "send", label: "Send", style: "primary", confirm: null, input: null, navigate: null },
        { id: "decline", label: "Decline", style: null, confirm: null, input: null, navigate: null },
      ],
    };
    const { props, rerender } = setup({ items: [agentAsk] });
    const pinned = row(agentAsk.id);
    expect(within(pinned).getByText(reason)).toBeInTheDocument();
    expect(within(pinned).getByText("Run the tests again")).toBeInTheDocument();
    await userEvent.click(within(pinned).getByRole("button", { name: "Send" }));
    expect(props.onRunAction).toHaveBeenCalledWith(agentAsk.id, "send", {});
    await userEvent.click(within(pinned).getByText("Run the tests again"));
    expect(props.onNavigate).toHaveBeenCalledWith({ kind: "session", id: "s1", roomId: null });

    const sent: ActivityItem = { ...agentAsk, state: "resolved", actions: [],
      summary: { ...agentAsk.summary, title: "Sent · Friday wants to message Fix login" } };
    rerender(<ActivityFeed {...props} items={[sent]} />);
    const done = row(sent.id);
    expect(within(done).getByText(/Sent · Friday wants to message Fix login/)).toBeInTheDocument();
    expect(within(done).queryByRole("button", { name: "Send" })).toBeNull();
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

  it("shows the connector hint above a login ask's fields, and nothing without one", async () => {
    const hint = "Google is connected. This password is only for the browser.";
    const hinted: ActivityItem = { ...loginAsk, id: "login:h1",
      actions: [{ ...loginAsk.actions[0], input: { ...loginAsk.actions[0].input!, hint } }, loginAsk.actions[1]] };
    setup({ items: [hinted, loginAsk] });
    await userEvent.click(within(row(hinted.id)).getByRole("button", { name: "Add login" }));
    const form = within(row(hinted.id)).getByLabelText("Password").closest("form")!;
    expect(form.firstElementChild).toHaveTextContent(hint);
    await userEvent.click(within(row(loginAsk.id)).getByRole("button", { name: "Add login" }));
    expect(within(row(loginAsk.id)).queryByText(hint)).not.toBeInTheDocument();
  });

  it("takes a login an agent asked for in a masked form and posts it as the value only", async () => {
    const { props } = setup({ items: [loginAsk, mention] });
    const pinned = row(loginAsk.id);
    expect(pinned.closest("section")).toHaveAttribute("data-section", "needs-you");
    await userEvent.click(within(pinned).getByRole("button", { name: "Add login" }));
    const password = within(pinned).getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    await userEvent.type(within(pinned).getByLabelText("Username or email"), "me@example.com");
    await userEvent.type(password, "s3cret!");
    const submits = within(pinned).getAllByRole("button", { name: "Add login" });
    await userEvent.click(submits[submits.length - 1]);
    expect(props.onRunAction).toHaveBeenCalledWith(loginAsk.id, "provide",
      { value: JSON.stringify({ username: "me@example.com", password: "s3cret!" }) });
  });
});
