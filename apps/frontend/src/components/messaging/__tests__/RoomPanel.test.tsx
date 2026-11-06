import { useState } from "react";
import { ApiError } from "@/lib/api";
import { JoinLinkCopyError } from "../messageLinks";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { typeInComposer } from "@/test-utils/composer";
import type { RoomRunView } from "@lightbulb/shared";
import type { RoomMessage, RoomParticipant } from "../types";

// Search results come from the server; the query picks which hit comes back.
const searchHits = vi.hoisted(() => ({ byQuery: {} as Record<string, unknown[]> }));
vi.mock("../useMessageSearch", () => ({
  useMessageSearch: (query: string) => {
    const hits = searchHits.byQuery[query.trim()] ?? [];
    return { hits, state: hits.length ? "done" : "idle" };
  },
}));
const hitFor = (m: RoomMessage) => ({ messageId: m.messageId, roomId: m.roomId, messageSeq: m.messageSeq, threadRootMessageId: m.threadRootMessageId,
  authorName: "Friday", authorKind: "agent", createdAt: m.createdAt, snippet: m.text });

vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));

// RoomPanel reads the narrow breakpoint straight off matchMedia (tests/setup.ts
// stubs "nothing matches", i.e. the wide layout). Call narrowViewport() in a
// test that needs the stacked layout; the afterEach puts the wide stub back.
const wideMatchMedia = window.matchMedia;
const narrowViewport = () => {
  window.matchMedia = ((query: string) => ({
    matches: query === "(max-width: 859px)",
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  })) as unknown as typeof window.matchMedia;
};
afterEach(() => { window.matchMedia = wideMatchMedia; searchHits.byQuery = {}; });

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member", agentActivity: "running" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Old name" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [owner, friday], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
};

it("confirms removal of a non-owner with Cancel focused and restores focus", async () => {
  const user = userEvent.setup();
  const onRemoveMember = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onRemoveMember={onRemoveMember} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  expect(screen.queryByRole("button", { name: "Remove Owner" })).not.toBeInTheDocument();
  const trigger = screen.getByRole("button", { name: "Remove Friday" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Remove member" });
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
  expect(dialog).toHaveTextContent("Remove Friday from Launch?");
  await user.keyboard("{Escape}");
  expect(onRemoveMember).not.toHaveBeenCalled();
  expect(trigger).toHaveFocus();
  await user.click(trigger);
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Remove member" }));
  expect(onRemoveMember).toHaveBeenCalledWith({ kind: "agent", id: "friday" });
});

it.each(["dm", "group-dm", "channel"] as const)("does not expose management without room admin permission (%s)", async (kind) => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind }}
    participants={[{ ...owner, role: "member" }, friday]} onRenameRoom={vi.fn()} onRemoveMember={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: /^About/ }));
  expect(screen.queryByRole("button", { name: /Rename (channel|group chat)/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Remove Friday" })).not.toBeInTheDocument();
});

it("opens a thread-reply hit in its thread and jumps to a main-list hit", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const onSelectThread = vi.fn();
  const onJumpToMessage = vi.fn();
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Searchable reply", threadRootMessageId: message.messageId };
  searchHits.byQuery = { searchable: [hitFor(reply)], hello: [hitFor(message)] };
  const view = render(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={null} onSelectThread={onSelectThread} onJumpToMessage={onJumpToMessage} />);
  await userEvent.click(screen.getByRole("button", { name: "Search this chat" }));
  await userEvent.type(screen.getByRole("searchbox"), "searchable");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(onSelectThread).toHaveBeenLastCalledWith(message.messageId));
  expect(onJumpToMessage).not.toHaveBeenCalled();
  view.rerender(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={message.messageId} onSelectThread={onSelectThread} onJumpToMessage={onJumpToMessage} />);
  const thread = screen.getByRole("complementary", { name: "Thread" });
  expect(within(thread).getByText("Searchable reply").closest("article")).toHaveFocus();
  await userEvent.click(screen.getByRole("button", { name: "Search this chat" }));
  await userEvent.type(screen.getByRole("searchbox"), "hello");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(onSelectThread).toHaveBeenLastCalledWith(null));
  expect(onJumpToMessage).toHaveBeenCalledWith("room", message.messageId);
});

it("loads missing replies when a thread opens from a bounded history page", () => {
  const onLoadHistory = vi.fn();
  const view = render(<RoomPanel {...props} selectedThreadId={message.messageId}
    historyComplete={false} historyLoading={false} onLoadHistory={onLoadHistory} />);
  expect(onLoadHistory).toHaveBeenCalledOnce();
  view.rerender(<RoomPanel {...props} selectedThreadId={message.messageId}
    historyComplete={false} historyLoading={false} onLoadHistory={onLoadHistory} />);
  expect(onLoadHistory).toHaveBeenCalledOnce();
});

it("does not leave focus on a stale thread trigger when a search result jumps to a main message", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const onJumpToMessage = vi.fn();
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Reply text", threadRootMessageId: message.messageId };
  searchHits.byQuery = { hello: [hitFor(message)] };
  render(<RoomPanel {...props} messages={[message, reply]} onJumpToMessage={onJumpToMessage} />);
  const replyTrigger = screen.getByRole("button", { name: "Reply in thread" });
  await userEvent.click(replyTrigger);
  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Search this chat" }));
  await userEvent.type(screen.getByRole("searchbox"), "hello");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument());
  expect(onJumpToMessage).toHaveBeenCalledWith("room", message.messageId);
  expect(replyTrigger).not.toHaveFocus();
});

it("scrolls to a main-list hit again when the same hit is picked twice", async () => {
  const scroll = vi.fn();
  HTMLElement.prototype.scrollIntoView = scroll;
  searchHits.byQuery = { hello: [hitFor(message)] };
  render(<RoomPanel {...props} onJumpToMessage={vi.fn()} />);
  for (const n of [1, 2]) {
    await userEvent.click(screen.getByRole("button", { name: "Search this chat" }));
    await userEvent.type(screen.getByRole("searchbox"), "hello");
    await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
    await waitFor(() => expect(scroll).toHaveBeenCalledTimes(n));
  }
});

it("names a DM for its counterpart and uses canonical author and member activity", () => {
  render(<RoomPanel {...props} />);
  expect(screen.getByRole("region", { name: "Friday" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Friday" })).toBeInTheDocument();
  const article = screen.getByRole("article", { name: "Message from Friday" });
  expect(within(article).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  expect(within(article).getByText("Agent")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "People, 2 here" }));
  const members = screen.getByRole("complementary", { name: "About" });
  expect(within(members).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  expect(within(members).queryByRole("status", { name: "Work status" })).not.toBeInTheDocument();
});

it("opens a notification-selected thread and closes it when selection is cleared", () => {
  const messages = [message, { ...message, messageId: "reply", messageSeq: 2, text: "Notification thread reply", threadRootMessageId: message.messageId }];
  const view = render(<RoomPanel {...props} messages={messages} selectedThreadId={null} />);
  expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument();
  view.rerender(<RoomPanel {...props} messages={messages} selectedThreadId={message.messageId} />);
  expect(within(screen.getByRole("complementary", { name: "Thread" })).getByText("Notification thread reply")).toBeInTheDocument();
  view.rerender(<RoomPanel {...props} messages={messages} selectedThreadId={null} />);
  expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument();
});

it("keeps empty reaction and reply rows out of the message and groups its actions", () => {
  const ownMessage = { ...message, author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[ownMessage]} onSetReaction={vi.fn()} onUpdateMessage={vi.fn()} />);
  const article = screen.getByRole("article", { name: "Message from Owner" });
  const actions = within(article).getByRole("toolbar", { name: "Message tools" });
  expect(within(actions).getByRole("button", { name: "Add reaction" })).toBeInTheDocument();
  expect(within(actions).getByRole("button", { name: "Reply in thread" })).toBeInTheDocument();
  expect(within(actions).getByRole("button", { name: "Message actions" })).toBeInTheDocument();
  // W7: one tab stop per message, "Message actions"; the arrow keys walk the rest.
  const stops = within(actions).getAllByRole("button").filter((button) => button.tabIndex === 0);
  expect(stops.map((button) => button.getAttribute("aria-label"))).toEqual(["Message actions"]);
  fireEvent.keyDown(within(actions).getByRole("button", { name: "Message actions" }), { key: "Home" });
  expect(document.activeElement).toBe(within(actions).getAllByRole("button")[0]);
  expect(within(actions).getAllByRole("button").filter((button) => button.tabIndex === 0)).toEqual([document.activeElement]);
  expect(within(article).queryByLabelText("Message reactions")).not.toBeInTheDocument();
  expect(article.querySelector(".room-reply")).toBeNull();
  expect(within(article).queryByText("Reply in thread")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog", { name: "Choose an emoji" })).not.toBeInTheDocument();
});

it("opens the reaction picker with the keyboard and leaves failures outside the toolbar", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const onSetReaction = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomPanel {...props} onSetReaction={onSetReaction} />);
  const add = screen.getByRole("button", { name: "Add reaction" });
  act(() => add.focus());
  expect(add).toHaveFocus();
  await user.keyboard("{Enter}");
  const dialog = screen.getByRole("dialog", { name: "Choose an emoji" });
  await user.type(within(dialog).getByRole("searchbox", { name: "Search emoji" }), "thumbs up");
  await user.keyboard("{ArrowDown}{Enter}");
  expect(onSetReaction).toHaveBeenCalledWith("message", "👍", true);
  expect(add).toBeDisabled();
  await act(async () => reject(new Error("Unavailable")));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not update reaction");
  expect(screen.getByRole("alert").closest(".room-message-actions")).toBeNull();
  expect(add).toBeEnabled();
  const reply = screen.getByRole("button", { name: "Reply in thread" });
  act(() => reply.focus());
  await user.keyboard("{Enter}");
  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
});

it("shows only applied reaction chips and a nonzero reply count below the message", () => {
  render(<RoomPanel {...props} onSetReaction={vi.fn()} messages={[
    { ...message, reactions: { "👍": ["owner", "other"], "❤️": [], "✅": ["other"] } },
    { ...message, messageId: "reply", text: "Reply", threadRootMessageId: message.messageId },
  ]} />);
  const article = screen.getByRole("article", { name: "Message from Friday" });
  const reactions = within(article).getByLabelText("Message reactions");
  expect(within(reactions).getAllByRole("button")).toHaveLength(2);
  expect(within(reactions).getByRole("button", { name: "Remove 👍 reaction, 2" })).toHaveAttribute("aria-pressed", "true");
  expect(within(reactions).getByRole("button", { name: "Add ✅ reaction, 1" })).toBeInTheDocument();
  expect(article.querySelector(".room-reply")).toHaveTextContent("1 reply");
});

it("shows quick reactions and a picker in the toolbar and renders any applied emoji as a chip", async () => {
  const user = userEvent.setup();
  const onSetReaction = vi.fn(async () => {});
  render(<RoomPanel {...props} messages={[{ ...message, reactions: { "🚀": ["owner"], "🙏": ["someone"] } }]} onSetReaction={onSetReaction} />);
  expect(screen.getByRole("button", { name: "Remove 🚀 reaction, 1" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "Add 🙏 reaction, 1" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "React 👍" }));
  expect(onSetReaction).toHaveBeenCalledWith("message", "👍", true);
  await user.click(screen.getByRole("button", { name: "Add reaction" }));
  expect(screen.getByRole("dialog", { name: "Choose an emoji" })).toBeInTheDocument();
});

it("shows persisted reactions and removes the viewer's reaction from a thread", async () => {
  const onSetReaction = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} onSetReaction={onSetReaction} messages={[{ ...message, reactions: { "👍": ["owner", "other"] } }]} />);
  expect(screen.getByRole("button", { name: "Remove 👍 reaction, 2" })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(screen.getByRole("button", { name: "Reply in thread" }));
  await userEvent.click(within(screen.getByRole("complementary", { name: "Thread" })).getByRole("button", { name: "Remove 👍 reaction, 2" }));
  expect(onSetReaction).toHaveBeenCalledWith("message", "👍", false);
});

it("excludes only the signed-in human when naming a group DM", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm" }} participants={[owner, { ...owner, id: "alice", displayName: "Alice" }, { ...friday, id: "owner", displayName: "Atlas" }]} messages={[]} />);
  expect(screen.getByRole("heading", { name: "Alice and Atlas" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Alice and Atlas" })).toBeInTheDocument();
});

it("opens profiles before the real DM action for human and agent authors and displays local time", async () => {
  const onSelectParticipant = vi.fn();
  const colleague = { ...owner, id: "colleague", displayName: "Aden" };
  render(<RoomPanel {...props} onSelectParticipant={onSelectParticipant} participants={[owner, friday, colleague]} messages={[message, { ...message, messageId: "human", author: { kind: "human", id: "colleague", displayName: "Aden" } }]} />);
  for (const participant of [friday, colleague]) {
    await userEvent.click(within(screen.getByRole("article", { name: `Message from ${participant.displayName}` })).getByRole("button", { name: participant.displayName }));
    const profile = screen.getByRole("dialog", { name: participant.displayName });
    await userEvent.click(within(profile).getByRole("button", { name: "Message" }));
    expect(onSelectParticipant).toHaveBeenLastCalledWith(participant);
  }
  const timestamp = within(screen.getByRole("article", { name: "Message from Friday" })).getByText(new Date(message.createdAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
  expect(timestamp).toHaveAttribute("datetime", message.createdAt);
});

it("opens and closes the real room context on a narrow viewport", async () => {
  narrowViewport();
  render(<RoomPanel {...props} />);
  const history = screen.getByRole("region", { name: "Messages" });
  await userEvent.click(screen.getByRole("button", { name: /^About/ }));
  expect(history).not.toBeVisible();
  expect(screen.getByRole("complementary", { name: "About" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "About" })).toHaveFocus();
  await userEvent.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(history).toBeVisible();
  expect(screen.getByRole("button", { name: /^About/ })).toHaveFocus();
});

it("closes the room details panel and restores its trigger on Escape", async () => {
  narrowViewport();
  render(<RoomPanel {...props} />);
  const history = screen.getByRole("region", { name: "Messages" });
  await userEvent.click(screen.getByRole("button", { name: /^About/ }));
  const details = screen.getByRole("complementary", { name: "About" });
  expect(history).not.toBeVisible();
  fireEvent.keyDown(details, { key: "Escape" });
  expect(screen.queryByRole("complementary", { name: "About" })).not.toBeInTheDocument();
  expect(history).toBeVisible();
  expect(screen.getByRole("button", { name: /^About/ })).toHaveFocus();
});

it("returns focus to the Details trigger, not <body>, when Escape closes the room details panel at wide width", async () => {
  // The default matchMedia stub (tests/setup.ts) matches nothing, so this
  // renders as the docked (>=1180px) layout -- the restore
  // effect used to gate its close branch on `isNarrow` and dropped focus to
  // <body> here.
  render(<RoomPanel {...props} />);
  await userEvent.click(screen.getByRole("button", { name: /^About/ }));
  const details = screen.getByRole("complementary", { name: "About" });
  fireEvent.keyDown(details, { key: "Escape" });
  expect(screen.queryByRole("complementary", { name: "About" })).not.toBeInTheDocument();
  const trigger = screen.getByRole("button", { name: /^About/ });
  expect(document.activeElement).toBe(trigger);
  expect(trigger).toHaveFocus();
});

it.each(["system", "human"] as const)("keeps unmatched %s authors neutral", (kind) => {
  render(<RoomPanel {...props} messages={[{ ...message, author: { kind, id: "friday", displayName: "Announcement" } }]} />);
  const article = screen.getByRole("article", { name: "Message from Announcement" });
  expect(within(article).getByRole("img", { name: "Announcement" }).parentElement).not.toHaveAttribute("data-agent-state");
  expect(within(article).queryByText(/^Agent /)).not.toBeInTheDocument();
});

it("shows canonical members and only running work in collapsible room details", async () => {
  const user = userEvent.setup();
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "active", state: "running", text: "", thinking: "", tools: [] };
  const { rerender } = render(<RoomPanel {...props} runs={[run, ...(["completed", "failed", "aborted"] as const).map((state) => ({ ...run, runId: state, state }))]} />);
  expect(screen.queryByRole("complementary", { name: "About" })).not.toBeInTheDocument();
  const toggle = screen.getByRole("button", { name: /^About/ });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  const details = screen.getByRole("complementary", { name: "About" });
  expect(within(details).getByText("Friday")).toBeInTheDocument();
  expect(within(details).getByText("Owner")).toBeInTheDocument();
  expect(within(details).getByText("Agent")).toBeInTheDocument();
  expect(within(details).getByText("Person")).toBeInTheDocument();
  expect(within(details).getAllByText("Friday is working")).toHaveLength(1);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await user.click(toggle);
  expect(screen.queryByRole("complementary", { name: "About" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^About/ })).toHaveFocus();
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  expect(toggle).toHaveFocus();
  rerender(<RoomPanel {...props} runs={[{ ...run, state: "completed" }]} />);
  expect(within(screen.getByRole("complementary", { name: "About" })).getByText("No active runs.")).toBeInTheDocument();
});

it("shows the newest reply's relative time beside the reply count", () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  try {
    vi.setSystemTime(new Date("2026-09-10T00:12:00Z"));
    render(<RoomPanel {...props} messages={[
      message,
      // The newest reply by messageSeq is listed FIRST in the array, and the
      // older one second -- a `replies.at(-1)`/last-array-element regression
      // would show reply-1's time (7m ago) instead of reply-2's (2m ago).
      { ...message, messageId: "reply-2", messageSeq: 3, text: "Second reply", threadRootMessageId: message.messageId, createdAt: "2026-09-10T00:10:00Z" },
      { ...message, messageId: "reply-1", messageSeq: 2, text: "First reply", threadRootMessageId: message.messageId, createdAt: "2026-09-10T00:05:00Z" },
    ]} />);
    const reply = screen.getByRole("button", { name: "Reply in thread, 2 replies · last reply 2m ago" });
    expect(reply).toHaveTextContent("2 replies · last reply 2m ago");
  } finally {
    vi.useRealTimers();
  }
});

it("focuses a thread and restores its trigger and collapsed preference on Escape", async () => {
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  render(<RoomPanel {...props} onSelectThread={onSelectThread} messages={[
    message,
    { ...message, messageId: "reply", messageSeq: 2, text: "Thread reply", threadRootMessageId: message.messageId, replyToMessageId: message.messageId },
  ]} />);
  const reply = screen.getByRole("button", { name: /^Reply in thread, 1 reply/ });
  await user.click(reply);
  expect(onSelectThread).toHaveBeenLastCalledWith("message");
  expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
  expect(screen.getByRole("region", { name: "Messages" })).toBeVisible();
  fireEvent.keyDown(screen.getByRole("complementary", { name: "Thread" }), { key: "Escape" });
  expect(onSelectThread).toHaveBeenLastCalledWith(null);
  expect(reply).toHaveFocus();
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^About/ })).toHaveAttribute("aria-expanded", "false");
});

it("keeps a thread open when Escape dismisses its delete dialog or deletion is pending", async () => {
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  let reject!: (error: Error) => void;
  const onUpdateMessage = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomPanel {...props} selectedThreadId={message.messageId} onSelectThread={onSelectThread} onUpdateMessage={onUpdateMessage}
    messages={[{ ...message, author: { kind: "human", id: "owner", displayName: "Owner" } }]} />);
  const thread = screen.getByRole("complementary", { name: "Thread" });
  await user.click(within(thread).getByRole("button", { name: "Message actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete message" }));
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(onSelectThread).not.toHaveBeenCalled();
  expect(thread).toBeInTheDocument();
  await user.click(within(thread).getByRole("button", { name: "Message actions" }));
  await user.click(screen.getByRole("menuitem", { name: "Delete message" }));
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Delete message" }));
  await user.keyboard("{Escape}");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(onSelectThread).not.toHaveBeenCalled();
  expect(thread).toBeInTheDocument();
  await act(async () => reject(new Error("Retry deletion")));
});

it("keeps a live thread reply out of main chat and renders it inside the thread", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "thread-run", threadRootMessageId: message.messageId,
    state: "running", text: "Thread answer", thinking: "", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(within(screen.getByRole("region", { name: "Messages" })).queryByRole("article", { name: "Live run from Friday" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reply in thread" })).toBeInTheDocument();

  await userEvent.click(screen.getByRole("button", { name: "Reply in thread" }));

  expect(within(screen.getByRole("complementary", { name: "Thread" })).getByRole("article", { name: "Live run from Friday" })).toHaveTextContent("Thread answer");
});

it("collapses a finished run with no reply into a muted status line", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(screen.getByText("Friday finished · no reply")).toBeInTheDocument();
  expect(screen.queryByRole("article", { name: "Live run from Friday" })).not.toBeInTheDocument();
});

// What a client that opened the room mid-run holds: a run seeded from the room
// detail's `activeRuns`, with no frame text yet. The no-text collapse must not
// swallow it — that is the "Forge is working" bubble and its Stop.
it("renders a running run that has no text yet as a working bubble with Stop and no steer box", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "running", tools: [] };
  render(<RoomPanel {...props} runs={[run]} onAbortRun={vi.fn()} />);
  const article = screen.getByRole("article", { name: "Live run from Friday" });
  expect(within(article).getByRole("button", { name: "Stop Friday" })).toHaveTextContent("Stop");
  expect(within(article).getByRole("button", { name: "Stop Friday" })).toHaveAttribute("title", "Stop Friday");
  // In the header row (far right), not on a line of its own.
  expect(within(article).getByRole("button", { name: "Stop Friday" }).closest(".room-message-byline")).not.toBeNull();
  // The composer is the steer: a follow-up is delivered into the live run by the backend.
  expect(within(article).queryByRole("textbox")).not.toBeInTheDocument();
  expect(screen.queryByText("Friday finished · no reply")).not.toBeInTheDocument();
});

it("says a stop the runtime never confirmed may still be finishing its step", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "aborted", forcedStop: true, tools: [] };
  render(<RoomPanel {...props} runs={[run]} onAbortRun={vi.fn()} />);
  expect(screen.getByText("Friday stopped (the agent may finish its current step)")).toBeInTheDocument();
});

it("shows Stopping… until the run ends, and gives Stop back if the request fails", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "running", tools: [] };
  let fail: (error: Error) => void = () => {};
  const onAbortRun = vi.fn(() => new Promise((_, reject) => { fail = reject; }));
  render(<RoomPanel {...props} runs={[run]} onAbortRun={onAbortRun} />);
  fireEvent.click(screen.getByRole("button", { name: "Stop Friday" }));
  expect(onAbortRun).toHaveBeenCalledWith("run-1");
  expect(screen.getByRole("button", { name: "Stopping Friday…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Stopping Friday…" })).toHaveTextContent("Stopping…");
  await act(async () => fail(new Error("503")));
  expect(screen.getByRole("button", { name: "Stop Friday" })).toBeEnabled();
});

it("shows one working card per agent, a waiting note on the queued follow-up and a receipt on a steered one", () => {
  const queued: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-2", triggerMessageId: "m2", text: "", thinking: "", state: "running", tools: [] };
  const live: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", triggerMessageId: "m1", text: "Looking", thinking: "", state: "running", tools: [], steeredMessageIds: ["m3"] };
  const messages = ["m1", "m2", "m3"].map((messageId, index) => ({ ...props.messages[0], messageId, messageSeq: 100 + index, text: `ask ${index}` }));
  render(<RoomPanel {...props} messages={messages} runs={[queued, live]} onAbortRun={vi.fn()} />);
  expect(screen.getAllByRole("article", { name: "Live run from Friday" })).toHaveLength(1);
  expect(screen.getByText("Friday will read this after the current reply")).toBeInTheDocument();
  expect(screen.getByText("Friday saw this")).toBeInTheDocument();
});

// Prod 2026-09-25: Friday's stopped run (its text never saved) stayed pinned below every later
// message, so the owner's answer rendered ABOVE Friday's question and read as missing.
it("renders a message sent after a run ended below that run, with the new working card last", () => {
  const ask = { ...message, messageId: "ask", messageSeq: 129, author: { kind: "human" as const, id: "user", displayName: "You" }, text: "can we get back to the demos?" };
  const answer = { ...ask, messageId: "answer", messageSeq: 130, text: "it should be admin@thelightbulb.company" };
  const stopped: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", afterSeq: 129, text: "Which email should I use?", thinking: "", state: "aborted", tools: [] };
  const live: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-2", afterSeq: 130, text: "", thinking: "", state: "running", tools: [] };
  render(<RoomPanel {...props} messages={[ask, answer]} runs={[stopped, live]} />);
  const order = within(screen.getByRole("region", { name: "Messages" })).getAllByRole("article").map((node) => node.textContent ?? "");
  expect(order.findIndex((text) => text.includes("can we get back"))).toBeLessThan(order.findIndex((text) => text.includes("Which email")));
  expect(order.findIndex((text) => text.includes("Which email"))).toBeLessThan(order.findIndex((text) => text.includes("admin@thelightbulb.company")));
  expect(order.at(-1)).toContain("Working");
});

// The browser's native <details>/<summary> marker — a large unstyled triangle
// that ignored the design system — was the loudest thing in the transcript.
// Thinking is now the same quiet chevron button the tool trace uses.
it("puts a run's thinking behind a quiet chevron button, not a native disclosure", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "Weighing two options", state: "running", tools: [] };
  const { container } = render(<RoomPanel {...props} runs={[run]} />);
  expect(container.querySelector("details")).toBeNull();
  const toggle = screen.getByRole("button", { name: "Thinking" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByText("Weighing two options")).not.toBeInTheDocument();
  await userEvent.click(toggle);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(screen.getByText("Weighing two options")).toBeInTheDocument();
});

// The trace folds itself now, so wrapping it in a second "What it did"
// disclosure buried one summary line behind two clicks.
it("shows a finished no-reply run's tool activity as one folded line, not a nested disclosure", () => {
  const tools = [1, 2, 3].map((i) => ({ id: `t${i}`, name: "read", args: { path: `f${i}.ts` }, result: "ok", status: "done" as const }));
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "", thinking: "", state: "completed", tools };
  const { container } = render(<RoomPanel {...props} runs={[run]} />);
  expect(container.querySelector("details")).toBeNull();
  expect(screen.queryByText("What it did")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Read 3 files/ })).toBeInTheDocument();
});

it("keeps rendering a finished run's text instead of collapsing it", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "Partial answer", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(within(screen.getByRole("article", { name: "Live run from Friday" })).getByText("Partial answer")).toBeInTheDocument();
  expect(screen.queryByText("Friday finished · no reply")).not.toBeInTheDocument();
});

it("lets wide room details change agent response without giving humans a control", async () => {
  const user = userEvent.setup();
  const onListenModeChange = vi.fn();
  render(<RoomPanel {...props} onListenModeChange={onListenModeChange} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  const details = within(screen.getByRole("complementary", { name: "About" }));
  const mode = details.getByRole("combobox", { name: "Response for Friday" });
  expect(mode).toHaveValue("mentions");
  expect(within(mode).getAllByRole("option").map((option) => [option.getAttribute("value"), option.textContent])).toEqual([
    ["active", "Listening"], ["mentions", "Mentions"], ["observe", "Off"],
  ]);
  expect(details.getAllByRole("combobox")).toHaveLength(1);
  expect(details.queryByRole("combobox", { name: "Response for Owner" })).not.toBeInTheDocument();
  await user.selectOptions(mode, "observe");
  expect(onListenModeChange).toHaveBeenCalledWith("friday", "observe");
});

it("lets an ordinary member change an agent's response, and nothing else", async () => {
  const user = userEvent.setup();
  const onListenModeChange = vi.fn();
  render(<RoomPanel {...props} participants={[{ ...owner, role: "member" }, friday]}
    onRemoveMember={vi.fn()} onListenModeChange={onListenModeChange} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  const details = within(screen.getByRole("complementary", { name: "About" }));
  expect(details.queryByRole("button", { name: "Remove Friday" })).not.toBeInTheDocument();
  await user.selectOptions(details.getByRole("combobox", { name: "Response for Friday" }), "active");
  expect(onListenModeChange).toHaveBeenCalledWith("friday", "active");
});

it("shows an agent response as read-only text to ordinary members", async () => {
  const user = userEvent.setup();
  render(<RoomPanel {...props} participants={[{ ...owner, role: "member" }, friday]} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  const details = within(screen.getByRole("complementary", { name: "About" }));
  expect(details.getByText("Response: Mentions")).toBeInTheDocument();
  expect(details.queryByRole("combobox", { name: "Response for Friday" })).not.toBeInTheDocument();
});

it("adds only eligible non-members to an existing channel", async () => {
  const user = userEvent.setup();
  const onAddMember = vi.fn(async () => undefined);
  render(<RoomPanel {...props}
    room={{ ...props.room!, kind: "channel", name: "Launch" }}
    addMemberCandidates={[
      { kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null },
      { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null },
    ]}
    onAddMember={onAddMember} />);

  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(within(screen.getByRole("complementary", { name: "About" })).getByRole("button", { name: "Add people" }));
  expect(screen.getByRole("checkbox", { name: "Colleague" })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Atlas" })).toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: "Owner" })).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: "Friday" })).not.toBeInTheDocument();

  await user.click(screen.getByRole("checkbox", { name: "Atlas" }));
  await user.click(screen.getByRole("button", { name: "Add selected" }));
  expect(onAddMember).toHaveBeenCalledWith({ kind: "agent", id: "atlas" });
});

it("offers the workspace join link for someone who is not a member yet", async () => {
  const user = userEvent.setup();
  const onCopyJoinLink = vi.fn(async () => undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onCopyJoinLink={onCopyJoinLink} />);

  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(screen.getByRole("button", { name: "Copy the workspace join link" }));
  expect(onCopyJoinLink).toHaveBeenCalledTimes(1);
  expect(await screen.findByText("Copied. Anyone who opens it and signs in joins this workspace.")).toBeInTheDocument();
});

it("sends an admin to Regenerate when the link cannot be shown, instead of rotating it", async () => {
  const user = userEvent.setup();
  const onCopyJoinLink = vi.fn(async () => { throw new JoinLinkCopyError("unavailable"); });
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onCopyJoinLink={onCopyJoinLink} />);

  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(screen.getByRole("button", { name: "Copy the workspace join link" }));
  expect(await screen.findByText(/can regenerate it under Settings → Members/)).toBeInTheDocument();
});

it("takes one copy at a time so a double click cannot mint two links", async () => {
  const user = userEvent.setup();
  let finish!: () => void;
  const onCopyJoinLink = vi.fn(() => new Promise<void>((resolve) => { finish = resolve; }));
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onCopyJoinLink={onCopyJoinLink} />);

  await user.click(screen.getByRole("button", { name: /^About/ }));
  const button = screen.getByRole("button", { name: "Copy the workspace join link" });
  await user.click(button);
  expect(button).toBeDisabled();
  await user.click(button);
  expect(onCopyJoinLink).toHaveBeenCalledTimes(1);
  finish();
  expect(await screen.findByText(/Copied\./)).toBeInTheDocument();
  expect(button).toBeEnabled();
});

it("tells a non-admin where the join link lives instead of a bare error", async () => {
  const user = userEvent.setup();
  const onCopyJoinLink = vi.fn(async () => { throw new ApiError(403, { detail: "Only a workspace admin" }); });
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onCopyJoinLink={onCopyJoinLink} />);

  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(screen.getByRole("button", { name: "Copy the workspace join link" }));
  expect(await screen.findByText("This workspace's join link is turned off. Ask a workspace admin for a new one.")).toBeInTheDocument();
});

it("opens the channel member picker directly from the header for an owner", async () => {
  const user = userEvent.setup();
  render(<RoomPanel {...props}
    room={{ ...props.room!, kind: "channel", name: "Launch" }}
    addMemberCandidates={[{ kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null }]}
    onAddMember={vi.fn()} />);

  await user.click(screen.getByRole("button", { name: "Add people" }));
  expect(screen.getByRole("searchbox", { name: "Search people" })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Atlas" })).toBeInTheDocument();
});

it.each([
  ["an ordinary member", { participants: [{ ...owner, role: "member" as const }, friday] }],
  ["a shared channel", { room: { ...props.room!, kind: "channel" as const, shared: true } }],
  ["a group DM", { room: { ...props.room!, kind: "group-dm" as const, name: "Launch" } }],
])("hides the direct Add people header action for %s", (_label, overrides) => {
  render(<RoomPanel {...props} {...overrides} addMemberCandidates={[]} onAddMember={vi.fn()} />);
  expect(screen.queryByRole("button", { name: "Add people" })).not.toBeInTheDocument();
});

it("shows member-add progress and a useful retryable error", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const onAddMember = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomPanel {...props}
    room={{ ...props.room!, kind: "channel", name: "Launch" }}
    addMemberCandidates={[{ kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null }]}
    onAddMember={onAddMember} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(within(screen.getByRole("complementary", { name: "About" })).getByRole("button", { name: "Add people" }));
  await user.click(screen.getByRole("checkbox", { name: "Colleague" }));
  await user.click(screen.getByRole("button", { name: "Add selected" }));
  expect(screen.getByRole("button", { name: "Adding…" })).toBeDisabled();
  await act(async () => reject(new Error("You do not have permission to add members.")));
  expect(screen.getByRole("alert")).toHaveTextContent("You do not have permission to add members.");
  expect(screen.getByRole("button", { name: "Add selected" })).toBeEnabled();
  await user.keyboard("{Escape}");
  await user.click(within(screen.getByRole("complementary", { name: "About" })).getByRole("button", { name: "Add people" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("resets member selection and picker state when the room changes", async () => {
  const user = userEvent.setup();
  const candidates = [{ kind: "agent" as const, id: "atlas", displayName: "Atlas", mentionHandle: null }];
  const view = render(<RoomPanel {...props} messages={[]} roomId="channel-a" room={{ ...props.room!, roomId: "channel-a", kind: "channel", name: "A" }}
    addMemberCandidates={candidates} onAddMember={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(within(screen.getByRole("complementary", { name: "About" })).getByRole("button", { name: "Add people" }));
  await user.click(screen.getByRole("checkbox", { name: "Atlas" }));
  expect(screen.getByRole("checkbox", { name: "Atlas" })).toBeChecked();

  view.rerender(<RoomPanel {...props} messages={[]} roomId="channel-b" room={{ ...props.room!, roomId: "channel-b", kind: "channel", name: "B" }}
    addMemberCandidates={candidates} onAddMember={vi.fn()} />);

  expect(screen.queryByRole("button", { name: "Add selected" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /^About/ }));
  await user.click(within(screen.getByRole("complementary", { name: "About" })).getByRole("button", { name: "Add people" }));
  expect(screen.getByRole("checkbox", { name: "Atlas" })).not.toBeChecked();
});

it.each(["dm", "group-dm"] as const)("opens a searchable Add people picker from a %s header menu", async (kind) => {
  const user = userEvent.setup();
  const onAddMember = vi.fn(async () => undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind, name: kind === "group-dm" ? "Launch" : null }}
    addMemberCandidates={[
      { kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null },
      { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null },
    ]}
    onAddMember={onAddMember} />);

  await user.click(screen.getByRole("button", { name: "Options" }));
  await user.click(screen.getByRole("menuitem", { name: "Add people" }));
  const search = screen.getByRole("searchbox", { name: "Search people" });
  await user.type(search, "atl");
  expect(screen.queryByRole("checkbox", { name: "Colleague" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("checkbox", { name: "Atlas" }));
  await user.click(screen.getByRole("button", { name: "Add selected" }));

  expect(onAddMember).toHaveBeenCalledWith({ kind: "agent", id: "atlas" });
  // One flow, in a dialog: no Details pane opens behind it, and it closes once added (W17-6).
  expect(screen.queryByRole("complementary", { name: "About" })).toBeNull();
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Add people" })).toBeNull());
});

it("W17-6: several picked in a DM start one group chat with all of them", async () => {
  const user = userEvent.setup();
  const onAddMember = vi.fn(async () => undefined);
  const onAddMembers = vi.fn(async () => undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "dm", name: null }}
    addMemberCandidates={[
      { kind: "agent", id: "lb", displayName: "Lightbulb", mentionHandle: "lightbulb" },
      { kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null },
    ]}
    onAddMember={onAddMember} onAddMembers={onAddMembers} />);
  await user.click(screen.getByRole("button", { name: "Options" }));
  await user.click(screen.getByRole("menuitem", { name: "Add people" }));
  const dialog = screen.getByRole("dialog", { name: "Add people" });
  expect(dialog).toHaveTextContent("Adding someone starts a new group chat with them. This chat stays private.");
  await user.click(within(dialog).getByRole("checkbox", { name: "Lightbulb" }));
  await user.click(within(dialog).getByRole("checkbox", { name: "Colleague" }));
  await user.click(within(dialog).getByRole("button", { name: "Add selected" }));
  expect(onAddMembers).toHaveBeenCalledWith([{ kind: "agent", id: "lb" }, { kind: "human", id: "colleague" }]);
  expect(onAddMember).not.toHaveBeenCalled();
});

it("replaces the narrow conversation only while a selected thread is open", async () => {
  const user = userEvent.setup();
  const onLinkToWork = vi.fn();
  narrowViewport();
  render(<RoomPanel {...props} onLinkToWork={onLinkToWork} />);
  const history = screen.getByRole("region", { name: "Messages" });
  expect(history).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Options" }));
  await user.click(await screen.findByRole("menuitem", { name: "Tasks" }));
  expect(history).not.toBeVisible();
  await user.click(screen.getByRole("button", { name: /All tasks in Tasks/ }));
  expect(onLinkToWork).toHaveBeenCalledOnce();
  await user.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(history).toBeVisible();
  const trigger = screen.getByRole("button", { name: "Reply in thread" });
  await user.click(trigger);
  expect(history).not.toBeVisible();
  expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Back to chat" }));
  expect(history).toBeVisible();
  expect(trigger).toHaveFocus();
});

it.each([860, 1100, 1179, 1180])("dismisses detail on conversation return only in the medium range at %ipx", async (width) => {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query === "(min-width: 860px) and (max-width: 1179px)" && width < 1180,
    media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as MediaQueryList));
  const user = userEvent.setup();
  render(<RoomPanel {...props} />);
  const history = screen.getByRole("region", { name: "Messages" });
  const reply = screen.getByRole("button", { name: "Reply in thread" });
  for (const returnBy of ["click", "focus"] as const) {
    await user.click(reply);
    expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
    if (returnBy === "click") await user.click(screen.getByRole("heading", { name: "Friday" }));
    else act(() => history.focus());
    if (width < 1180) {
      expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument();
      expect(reply).toHaveFocus();
    } else {
      expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
      if (returnBy === "focus") expect(history).toHaveFocus();
    }
  }
});

it("dismisses a medium overlay on Escape from outside the room and restores its trigger", async () => {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query === "(min-width: 860px) and (max-width: 1179px)",
    media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as MediaQueryList));
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  const { unmount } = render(<><button>Outside room</button><RoomPanel {...props} onSelectThread={onSelectThread} /></>);
  const reply = screen.getByRole("button", { name: "Reply in thread" });
  await user.click(reply);
  await user.click(screen.getByRole("button", { name: "Outside room" }));
  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument();
  expect(reply).toHaveFocus();
  expect(onSelectThread).toHaveBeenLastCalledWith(null);
  await user.click(reply);
  onSelectThread.mockClear();
  unmount();
  await user.keyboard("{Escape}");
  expect(onSelectThread).not.toHaveBeenCalled();
});

it("leaves a medium overlay open when Escape is dismissing a portalled popper", async () => {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query === "(min-width: 860px) and (max-width: 1179px)",
    media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as MediaQueryList));
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  render(<><button>Outside room</button><RoomPanel {...props} onSelectThread={onSelectThread} /></>);
  await user.click(screen.getByRole("button", { name: "Reply in thread" }));
  await user.click(screen.getByRole("button", { name: "Outside room" }));
  // A Radix menu/picker renders in a portal outside this subtree, so its own
  // Escape still bubbles to the window listener.
  const popper = document.body.appendChild(document.createElement("div"));
  popper.setAttribute("data-radix-popper-content-wrapper", "");
  const item = popper.appendChild(document.createElement("button"));
  item.focus();

  await user.keyboard("{Escape}");
  popper.remove();

  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
  expect(onSelectThread).not.toHaveBeenCalledWith(null);
});

it("leaves a medium overlay open when Escape is dismissing an open dialog", async () => {
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    matches: query === "(min-width: 860px) and (max-width: 1179px)",
    media: query, addEventListener: vi.fn(), removeEventListener: vi.fn(),
  } as unknown as MediaQueryList));
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  render(<><button>Outside room</button><RoomPanel {...props} onSelectThread={onSelectThread} /></>);
  await user.click(screen.getByRole("button", { name: "Reply in thread" }));
  await user.click(screen.getByRole("button", { name: "Outside room" }));
  const sheet = document.body.appendChild(document.createElement("div"));
  sheet.setAttribute("role", "dialog");

  await user.keyboard("{Escape}");
  sheet.remove();

  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
  expect(onSelectThread).not.toHaveBeenCalledWith(null);
});

it("collapses consecutive messages from the same author and reveals the time in the gutter", () => {
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} />);
  const articles = screen.getAllByRole("article", { name: "Message from Friday" });
  expect(articles).toHaveLength(2);
  expect(within(articles[0]).getByText("Friday")).toBeInTheDocument();
  expect(within(articles[1]).queryByText("Friday")).not.toBeInTheDocument();
  const time = new Date(second.createdAt).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  expect(within(articles[1]).getByText(time, { selector: ".room-message-gutter" })).toBeInTheDocument();
  expect(within(articles[1]).getByText(time, { selector: "time.sr-only" })).toBeInTheDocument();
});

it("keeps every action reachable on a collapsed message", async () => {
  const user = userEvent.setup();
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} onSetReaction={vi.fn(async () => {})} onSelectThread={vi.fn()} />);
  const collapsed = screen.getAllByRole("article", { name: "Message from Friday" })[1];
  await user.click(within(collapsed).getByRole("button", { name: "Reply in thread" }));
  expect(within(collapsed).getByRole("button", { name: "Add reaction" })).toBeInTheDocument();
});

it("renders @mentions as chips", () => {
  render(<RoomPanel {...props} participants={[owner, { ...friday, mentionHandle: "friday" }]} messages={[{ ...message, text: "ping @friday" }]} />);
  expect(screen.getByText("@friday")).toHaveClass("room-mention");
});

it("renders an agent-pasted attachment link as inert text, not a broken link", () => {
  // /api/attachments/{id}/content only resolves through the issue-scoped
  // Teams BFF — a room message has no owning issue to fetch it through, so
  // it must not render as a link to this app's own (404ing) host.
  render(<RoomPanel {...props} messages={[{ ...message, text: "See [the report](/api/attachments/att-1/content)" }]} />);
  expect(screen.getByText("the report")).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "the report" })).not.toBeInTheDocument();
});

it("marks a message that mentions the viewer for highlighting", () => {
  render(<RoomPanel {...props} messages={[{ ...message, mentions: [{ kind: "human", id: "owner" }] }]} />);
  expect(screen.getByRole("article", { name: "Message from Friday" })).toHaveClass("room-message-mentions-me");
});

it("keeps the New line when a send fails and clears it after a successful send", async () => {
  const onSubmit = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(undefined);
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Hi", author: { kind: "human" as const, id: "owner", displayName: "Owner" } };

  function Harness() {
    const [text, setText] = useState("");
    const [draftId, setDraftId] = useState(0);
    return <RoomPanel {...props} readSeqAtOpen={1} messages={[message, reply]}
      composer={{
        ...props.composer,
        text,
        onTextChange: setText,
        clientMessageId: `draft-${draftId}`,
        onSubmit: async (request) => {
          try {
            await onSubmit(request);
          } finally {
            setText("");
            setDraftId((value) => value + 1);
          }
        },
      }} />;
  }

  render(<Harness />);
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  typeInComposer("First try");
  // Synchronous dispatch (no await before the assertion): the send is still in flight,
  // so a fix that waits for it to resolve must not have flipped sentSinceOpen yet.
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).toHaveAttribute("contenteditable", "true"));
  expect(onSubmit).toHaveBeenCalledTimes(1);
  // The failed send has now fully settled (composer re-enabled): the New line must
  // still be here, not just synchronously right after the keydown.
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  typeInComposer("Second try");
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("separator", { name: "New messages" })).not.toBeInTheDocument());
});

it("copies a message permalink from the toolbar", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn(async () => {});
  // userEvent.setup() installs its own clipboard stub (getter-only); override
  // it after setup so the handler writes through our spy (see
  // IssueDetailPage.description.test.tsx for the same pattern).
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<RoomPanel {...props} />);
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("panel=chat&room=room&message=message"));
  expect(await screen.findByRole("status", { name: "Link copied" })).toBeInTheDocument();
});

it("shows an alert when copying the link fails, and clears it on the next successful copy", async () => {
  const user = userEvent.setup();
  const writeText = vi.fn().mockRejectedValueOnce(new Error("denied")).mockResolvedValueOnce(undefined);
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(<RoomPanel {...props} />);
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not copy the link.");
  await user.click(screen.getByRole("button", { name: "Copy link" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

it("opens the edit form for your last message when ArrowUp lands in an empty composer", () => {
  const own = { ...message, messageId: "own", messageSeq: 2, author: { kind: "human" as const, id: props.currentUserId, displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[message, own]} onUpdateMessage={vi.fn()} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
});

it("lets a second ArrowUp reopen the edit form for the same message after Cancel", async () => {
  const user = userEvent.setup();
  const own = { ...message, messageId: "own", messageSeq: 2, author: { kind: "human" as const, id: props.currentUserId, displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[message, own]} onUpdateMessage={vi.fn()} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Cancel edit" }));
  expect(screen.queryByRole("textbox", { name: "Edit message" })).not.toBeInTheDocument();
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getByRole("textbox", { name: "Edit message" })).toBeInTheDocument();
});

it("opens exactly one edit form when the ArrowUp target is also the open thread's root", () => {
  const own = { ...message, messageId: "own", messageSeq: 2, author: { kind: "human" as const, id: props.currentUserId, displayName: "Owner" } };
  render(<RoomPanel {...props} messages={[message, own]} selectedThreadId={own.messageId} onUpdateMessage={vi.fn()} />);
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "ArrowUp" });
  expect(screen.getAllByRole("textbox", { name: "Edit message" })).toHaveLength(1);
});

it("keeps one labelled About button in the header and shows the pinned count once, on the header's pin button", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, topic: null, starred: false, muted: false, sectionId: null }}
    pinnedMessageIds={["m-1", "m-2"]} />);
  const about = screen.getByRole("button", { name: "About" });
  expect(about.closest(".room-header")).not.toBeNull();
  expect(about).toHaveTextContent("About");
  expect(about).not.toHaveTextContent("2");
  expect(screen.getByRole("button", { name: "2 pinned messages" })).toHaveTextContent("2");
  // The old icon-only view switches are gone from the header.
  for (const name of ["Conversation", "Channel", "Details", "Ideas", "Browser", "Files", "Threads"]) {
    expect(screen.queryByRole("button", { name })).not.toBeInTheDocument();
  }
});

it("pins and unpins a message from its menu", async () => {
  const onSetPin = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} pinnedMessageIds={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
  expect(onSetPin).toHaveBeenCalledWith(message.messageId, true);
  rerender(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} pinnedMessageIds={[message.messageId]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Unpin" })).toBeInTheDocument();
});

it("disables the pin and mark-unread menu items while a pin request is in flight", async () => {
  let resolvePin!: () => void;
  const onSetPin = vi.fn(() => new Promise<void>((done) => { resolvePin = done; }));
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} onSetPin={onSetPin} onMarkUnreadFrom={onMarkUnreadFrom} messages={[message]} pinnedMessageIds={[]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
  // The menu closes on select (Radix default); reopen it while the request is still
  // unresolved to observe the disabled/pending state.
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  const pinItem = await screen.findByRole("menuitem", { name: "Pinning…" });
  expect(pinItem).toHaveAttribute("aria-disabled", "true");
  expect(screen.getByRole("menuitem", { name: "Marking unread…" })).toHaveAttribute("aria-disabled", "true");
  await act(async () => resolvePin());
  expect(await screen.findByRole("menuitem", { name: "Pin" })).not.toHaveAttribute("aria-disabled");
  expect(screen.getByRole("menuitem", { name: "Mark unread from here" })).not.toHaveAttribute("aria-disabled");
});

it("marks unread from a message using that message's sequence", async () => {
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} onMarkUnreadFrom={onMarkUnreadFrom} messages={[{ ...message, messageSeq: 7 }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Mark unread from here" }));
  expect(onMarkUnreadFrom).toHaveBeenCalledWith(7);
});

it("reports a failed pin without losing the message", async () => {
  const onSetPin = vi.fn().mockRejectedValue(new Error("A room can pin at most 50 messages."));
  render(<RoomPanel {...props} onSetPin={onSetPin} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "Message actions" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("A room can pin at most 50 messages.");
  expect(screen.getByText(message.text)).toBeInTheDocument();
});

it("omits mark-unread-from-here for the thread pane's root message", async () => {
  const onSetPin = vi.fn().mockResolvedValue(undefined);
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} selectedThreadId={message.messageId} onSetPin={onSetPin} onMarkUnreadFrom={onMarkUnreadFrom} />);
  const thread = within(screen.getByRole("complementary", { name: "Thread" }));
  // The trigger button lives inside the thread pane, but DropdownMenuContent is portalled
  // to the document body, so the opened menu's items are queried unscoped.
  await userEvent.click(thread.getByRole("button", { name: "Message actions" }));
  expect(await screen.findByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Mark unread from here" })).not.toBeInTheDocument();
});

it("omits mark-unread-from-here for a thread reply", async () => {
  const onSetPin = vi.fn().mockResolvedValue(undefined);
  const onMarkUnreadFrom = vi.fn().mockResolvedValue(undefined);
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Reply text", threadRootMessageId: message.messageId };
  render(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={message.messageId}
    onSetPin={onSetPin} onMarkUnreadFrom={onMarkUnreadFrom} />);
  const thread = within(screen.getByRole("complementary", { name: "Thread" }));
  const [, replyTrigger] = thread.getAllByRole("button", { name: "Message actions" });
  await userEvent.click(replyTrigger);
  expect(await screen.findByRole("menuitem", { name: "Pin" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Mark unread from here" })).not.toBeInTheDocument();
});

it("opens the details panel on the pins tab from the header pinned count", async () => {
  const onSetPin = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} messages={[message]} pinnedMessageIds={[message.messageId]} onSetPin={onSetPin} />);
  await userEvent.click(screen.getByRole("button", { name: "1 pinned message" }));
  const details = within(screen.getByRole("complementary", { name: "About" }));
  expect(details.getByRole("tab", { name: "Pins", selected: true })).toBeInTheDocument();
  expect(within(details.getByRole("list", { name: "Pinned messages" })).getByText("Hello")).toBeInTheDocument();
  await userEvent.click(details.getByRole("button", { name: "Unpin this message" }));
  expect(onSetPin).toHaveBeenCalledWith(message.messageId, false);
  await userEvent.click(details.getByRole("tab", { name: "Members" }));
  expect(details.getByText("Friday")).toBeInTheDocument();
});

it("jumps from a pinned message back to it in the conversation", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  render(<RoomPanel {...props} messages={[message]} pinnedMessageIds={[message.messageId]} />);
  await userEvent.click(screen.getByRole("button", { name: "1 pinned message" }));
  await userEvent.click(screen.getByRole("button", { name: /^Jump to message from Friday at / }));
  expect(screen.queryByRole("complementary", { name: "About" })).not.toBeInTheDocument();
  expect(screen.getByRole("article", { name: "Message from Friday" }).closest("[data-message-id]")).toHaveFocus();
});

it("offers Listening with no plan lock", async () => {
  const user = userEvent.setup();
  render(<RoomPanel {...props} onListenModeChange={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: /^About/ }));
  const details = within(screen.getByRole("complementary", { name: "About" }));
  expect(within(details.getByRole("combobox", { name: "Response for Friday" })).getByRole("option", { name: "Listening" })).toBeEnabled();
  expect(details.queryByRole("link", { name: "See plans" })).not.toBeInTheDocument();
});


it("exposes a working thread before its first reply, then keeps unique responder avatars", async () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "thread-run", threadRootMessageId: message.messageId,
    state: "running", text: "", thinking: "", tools: [] };
  const view = render(<RoomPanel {...props} runs={[run]} />);
  const activity = screen.getByRole("button", { name: "Reply in thread, Friday is working…" });
  expect(within(activity).getByRole("img", { name: "Friday" })).toBeInTheDocument();
  await userEvent.click(activity);
  expect(within(screen.getByRole("complementary", { name: "Thread" })).getByRole("article", { name: "Live run from Friday" })).toBeInTheDocument();
  view.rerender(<RoomPanel {...props} runs={[]} messages={[message,
    { ...message, messageId: "reply-a", messageSeq: 2, threadRootMessageId: message.messageId },
    { ...message, messageId: "reply-b", messageSeq: 3, threadRootMessageId: message.messageId },
    { ...message, messageId: "reply-c", messageSeq: 4, author: { kind: "human", id: "owner", displayName: "Owner" }, threadRootMessageId: message.messageId },
  ]} />);
  const replies = screen.getByRole("button", { name: /^Reply in thread, 3 replies/ });
  expect(within(replies).getAllByRole("img")).toHaveLength(2);
  expect(within(replies).getByRole("img", { name: "Friday" })).toBeInTheDocument();
  expect(within(replies).getByRole("img", { name: "Owner" })).toBeInTheDocument();
  expect(replies).not.toHaveTextContent("working");
});

it("replaces the composer in an archived channel with an Unarchive action for admins only", async () => {
  const archived = { ...props.room!, kind: "channel" as const, name: "Launch", archivedAt: "2026-09-23T00:00:00Z" };
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomPanel {...props} room={archived} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This channel is archived. Unarchive it to post.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: "Message" })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Unarchive channel" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(false));

  rerender(<RoomPanel {...props} room={archived} participants={[{ ...owner, role: "member" }, friday]} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This channel is archived.")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Unarchive channel" })).not.toBeInTheDocument();
});

it("offers no top-level composer in the workspace feed: people reply in a post's thread", () => {
  const feed = { ...props.room!, kind: "channel" as const, name: "feed", feed: true };
  render(<RoomPanel {...props} room={feed} />);
  expect(screen.getByText("Only agents post to the feed. Reply to a post in its thread.")).toBeInTheDocument();
  expect(screen.queryByRole("textbox", { name: /message/i })).not.toBeInTheDocument();
});

it("lets a plain member unarchive an archived group chat", async () => {
  const archived = { ...props.room!, kind: "group-dm" as const, name: "Trio", archivedAt: "2026-09-23T00:00:00Z" };
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} room={archived} participants={[{ ...owner, role: "member" }, friday]} onSetArchived={onSetArchived} />);
  expect(screen.getByText("This group chat is archived. Unarchive it to post.")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Unarchive group chat" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(false));
});

it("shows the pre-typed replies in the starter channel until a human has written", async () => {
  const onTextChange = vi.fn();
  const panelProps = { ...props, room: { ...props.room!, isDefault: true }, composer: { ...props.composer, onTextChange } };
  const view = render(<RoomPanel {...panelProps} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "I'm starting" }));
  expect(onTextChange).toHaveBeenCalledWith("I'm starting ");
  const human = { ...message, messageId: "h1", messageSeq: 2, author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  view.rerender(<RoomPanel {...panelProps} messages={[message, human]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("hides the pre-typed replies when the welcome is not the oldest loaded message", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[{ ...message, messageSeq: 101 }]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("hides the pre-typed replies while a human message is pending", () => {
  const pending = { clientMessageId: "c1", roomId: props.roomId, threadRootMessageId: null, text: "hi", state: "sending" } as never;
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[message]} pendingMessages={[pending]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("keeps a draft the person already typed when a pre-typed reply is tapped", async () => {
  const onTextChange = vi.fn();
  const panelProps = { ...props, room: { ...props.room!, isDefault: true }, composer: { ...props.composer, text: "hello", onTextChange } };
  render(<RoomPanel {...panelProps} messages={[message]} />);
  await userEvent.click(screen.getByRole("button", { name: "I'm starting" }));
  expect(onTextChange).not.toHaveBeenCalled();
});

it("never shows the pre-typed replies outside the starter channel", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, isDefault: false }} messages={[message]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("with the switch, shows the pre-typed replies after a message the owner sent before setup finished", () => {
  const early = { ...message, messageId: "h0", author: { kind: "human" as const, id: "owner", displayName: "Owner" } };
  const welcome = { ...message, messageSeq: 2, kind: "first_run" };
  const room = { ...props.room!, kind: "dm" as const, firstRun: true };
  const view = render(<RoomPanel {...props} room={room} messages={[early, welcome]} />);
  expect(screen.getByRole("button", { name: "I'm starting" })).toBeInTheDocument();
  const answer = { ...early, messageId: "h1", messageSeq: 3 };
  view.rerender(<RoomPanel {...props} room={{ ...room, firstRun: false }} messages={[early, welcome, answer]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("with the switch, shows the pre-typed replies in the first-run Lightbulb DM and not under #general's team line", () => {
  const view = render(<RoomPanel {...props} room={{ ...props.room!, kind: "dm", firstRun: true }} messages={[{ ...message, kind: "first_run" }]} />);
  expect(screen.getByRole("button", { name: "I'm starting" })).toBeInTheDocument();
  view.rerender(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[{ ...message, kind: "team_welcome" }]} />);
  expect(screen.queryByRole("button", { name: "I'm starting" })).toBeNull();
});

it("streams the CEO's first message in the starter channel until a human has written", () => {
  window.sessionStorage.clear();
  vi.useFakeTimers();
  try {
    const long = { ...message, text: "Welcome to Acme. I'm your CEO. ".repeat(6).trim() };
    render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} messages={[long]} />);
    expect(screen.queryByText(long.text)).toBeNull();
    act(() => { vi.advanceTimersByTime(8000); });
    expect(screen.getByText(long.text)).toBeInTheDocument();
  } finally { vi.useRealTimers(); }
});

it("gates Send on an incomplete reply only while the starter chips are shown", () => {
  const composer = { ...props.composer, text: "I'm starting" };
  const view = render(<RoomPanel {...props} room={{ ...props.room!, isDefault: true }} composer={composer} messages={[message]} />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
  view.rerender(<RoomPanel {...props} room={{ ...props.room!, isDefault: false }} composer={composer} messages={[message]} />);
  expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
});

it("opens the member picker once when the sidebar asks for Add people", async () => {
  const onAddPeopleOpened = vi.fn();
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm", name: "Launch" }}
    addMemberCandidates={[{ kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null }]}
    onAddMember={vi.fn()} openAddPeople onAddPeopleOpened={onAddPeopleOpened} />);
  expect(await screen.findByRole("searchbox", { name: "Search people" })).toBeInTheDocument();
  expect(onAddPeopleOpened).toHaveBeenCalledTimes(1);
});
