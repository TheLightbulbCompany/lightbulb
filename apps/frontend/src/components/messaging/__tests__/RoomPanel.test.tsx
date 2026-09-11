import { useState } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import type { RoomMessage, RoomParticipant, RoomRunView } from "../types";

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const friday: RoomParticipant = { ...owner, kind: "agent", id: "friday", displayName: "Friday", role: "member", agentActivity: "running" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "friday", displayName: "Old name" }, text: "Hello", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null, run: { state: "completed" } };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z" },
  participants: [owner, friday], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, friday], selectedMentions: [], onMentionsChange: vi.fn(), clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
};

it("renames a channel in a centered dialog with pending, retry, and focus restoration", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const onRenameRoom = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onRenameRoom={onRenameRoom} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  const trigger = screen.getByRole("button", { name: "Rename channel" });
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "Rename channel" });
  const input = within(dialog).getByRole("textbox", { name: "Channel name" });
  expect(input).toHaveFocus();
  expect(input).toHaveValue("Launch");
  await user.clear(input);
  await user.type(input, "   ");
  expect(within(dialog).getByRole("button", { name: "Save name" })).toBeDisabled();
  await user.type(input, "New name ");
  await user.click(within(dialog).getByRole("button", { name: "Save name" }));
  expect(onRenameRoom).toHaveBeenCalledWith("New name");
  expect(within(dialog).getByRole("button", { name: "Saving…" })).toBeDisabled();
  await user.keyboard("{Escape}");
  expect(dialog).toBeInTheDocument();
  await act(async () => reject(new Error("Could not save. Try again.")));
  expect(within(dialog).getByRole("alert")).toHaveTextContent("Could not save. Try again.");
  onRenameRoom.mockResolvedValue(undefined);
  await user.click(within(dialog).getByRole("button", { name: "Save name" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("lets a group chat admin rename the chat without exposing channel membership controls", async () => {
  const user = userEvent.setup();
  const onRenameRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm", name: "Launch crew" }}
    onRenameRoom={onRenameRoom} onAddMember={vi.fn()} onRemoveMember={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  expect(screen.queryByRole("button", { name: "Add members" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Remove Friday" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Rename group chat" }));
  const dialog = screen.getByRole("dialog", { name: "Rename group chat" });
  const input = within(dialog).getByRole("textbox", { name: "Group chat name" });
  expect(input).toHaveValue("Launch crew");
  await user.clear(input);
  await user.type(input, "Release room");
  await user.click(within(dialog).getByRole("button", { name: "Save name" }));
  expect(onRenameRoom).toHaveBeenCalledWith("Release room");
});

it("confirms removal of a non-owner with Cancel focused and restores focus", async () => {
  const user = userEvent.setup();
  const onRemoveMember = vi.fn().mockResolvedValue(undefined);
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "channel", name: "Launch" }} onRemoveMember={onRemoveMember} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
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
  await userEvent.click(screen.getByRole("button", { name: "Show room details" }));
  expect(screen.queryByRole("button", { name: /Rename (channel|group chat)/ })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Remove Friday" })).not.toBeInTheDocument();
});

it("never offers renaming for a direct message", async () => {
  render(<RoomPanel {...props} onRenameRoom={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Show room details" }));
  expect(screen.queryByRole("button", { name: /Rename/ })).not.toBeInTheDocument();
});

it("closes channel management if the viewer loses admin permission", async () => {
  const onRenameRoom = vi.fn();
  const channel = { ...props.room!, kind: "channel" as const };
  const { rerender } = render(<RoomPanel {...props} room={channel} onRenameRoom={onRenameRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Show room details" }));
  await userEvent.click(screen.getByRole("button", { name: "Rename channel" }));
  rerender(<RoomPanel {...props} room={channel} participants={[{ ...owner, role: "member" }, friday]} onRenameRoom={onRenameRoom} />);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(onRenameRoom).not.toHaveBeenCalled();
});

it("navigates search results into controlled threads and focuses main results", async () => {
  const scroll = vi.fn();
  HTMLElement.prototype.scrollIntoView = scroll;
  const onSelectThread = vi.fn();
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Searchable reply", threadRootMessageId: message.messageId };
  const view = render(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={null} onSelectThread={onSelectThread} historyComplete />);
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "searchable");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(onSelectThread).toHaveBeenLastCalledWith(message.messageId));
  view.rerender(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={message.messageId} onSelectThread={onSelectThread} historyComplete />);
  const thread = screen.getByRole("complementary", { name: "Thread" });
  expect(within(thread).getByText("Searchable reply").closest("article")).toHaveFocus();
  expect(scroll).toHaveBeenCalledWith({ block: "center" });
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "hello");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(onSelectThread).toHaveBeenLastCalledWith(null));
  view.rerender(<RoomPanel {...props} messages={[message, reply]} selectedThreadId={null} onSelectThread={onSelectThread} historyComplete />);
  expect(screen.getByRole("article", { name: "Message from Friday" }).closest("[data-message-id]")).toHaveFocus();
});

it("does not leave focus on a stale thread trigger when a search result targets a main message", async () => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "Reply text", threadRootMessageId: message.messageId };
  render(<RoomPanel {...props} messages={[message, reply]} />);
  const replyTrigger = screen.getByRole("button", { name: "Reply in thread" });
  await userEvent.click(replyTrigger);
  expect(screen.getByRole("complementary", { name: "Thread" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Search this conversation" }));
  await userEvent.type(screen.getByRole("searchbox"), "hello");
  await userEvent.click(within(screen.getByRole("list", { name: "Search results" })).getByRole("button"));
  await waitFor(() => expect(screen.queryByRole("complementary", { name: "Thread" })).not.toBeInTheDocument());
  expect(screen.getByRole("article", { name: "Message from Friday" }).closest("[data-message-id]")).toHaveFocus();
  expect(replyTrigger).not.toHaveFocus();
});

it("names a DM for its counterpart and uses canonical author and member activity", () => {
  render(<RoomPanel {...props} />);
  expect(screen.getByRole("region", { name: "Friday" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "Friday" })).toBeInTheDocument();
  const article = screen.getByRole("article", { name: "Message from Friday" });
  expect(within(article).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  expect(within(article).getByText("Agent")).toBeInTheDocument();
  expect(within(article).getByRole("status", { name: "Work status" })).toHaveTextContent("Completed");
  fireEvent.click(screen.getByRole("button", { name: "View members" }));
  const members = screen.getByRole("region", { name: "Room members" });
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
  const actions = within(article).getByRole("group", { name: "Message actions" });
  expect(within(actions).getByRole("button", { name: "Add reaction" })).toBeInTheDocument();
  expect(within(actions).getByRole("button", { name: "Reply in thread" })).toBeInTheDocument();
  expect(within(actions).getByRole("button", { name: "Message actions" })).toBeInTheDocument();
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
  const timestamp = within(screen.getByRole("article", { name: "Message from Friday" })).getByText(new Date(message.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
  expect(timestamp).toHaveAttribute("datetime", message.createdAt);
});

it("opens and closes the real room context on a narrow viewport", async () => {
  render(<RoomPanel {...props} narrow />);
  const history = screen.getByRole("region", { name: "Messages" });
  await userEvent.click(screen.getByRole("button", { name: "Show room details" }));
  expect(history).not.toBeVisible();
  expect(screen.getByRole("complementary", { name: "Room details" })).toBeVisible();
  expect(screen.getByRole("heading", { name: "Room details" })).toHaveFocus();
  await userEvent.click(screen.getByRole("button", { name: "Back to conversation" }));
  expect(history).toBeVisible();
  expect(screen.getByRole("button", { name: "Show room details" })).toHaveFocus();
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
  expect(screen.queryByRole("complementary", { name: "Room details" })).not.toBeInTheDocument();
  const toggle = screen.getByRole("button", { name: "Show room details" });
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  const details = screen.getByRole("complementary", { name: "Room details" });
  expect(within(details).getByText("Friday")).toBeInTheDocument();
  expect(within(details).getByText("Owner")).toBeInTheDocument();
  expect(within(details).getByText("Agent")).toBeInTheDocument();
  expect(within(details).getByText("Person")).toBeInTheDocument();
  expect(within(details).getAllByText("Friday is working")).toHaveLength(1);
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await user.click(toggle);
  expect(screen.queryByRole("complementary", { name: "Room details" })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Show room details" })).toHaveFocus();
  expect(toggle).toHaveAttribute("aria-expanded", "false");
  await user.click(toggle);
  expect(toggle).toHaveFocus();
  rerender(<RoomPanel {...props} runs={[{ ...run, state: "completed" }]} />);
  expect(within(screen.getByRole("complementary", { name: "Room details" })).getByText("No active runs.")).toBeInTheDocument();
});

it("focuses a thread and restores its trigger and collapsed preference on Escape", async () => {
  const user = userEvent.setup();
  const onSelectThread = vi.fn();
  render(<RoomPanel {...props} onSelectThread={onSelectThread} messages={[
    message,
    { ...message, messageId: "reply", messageSeq: 2, text: "Thread reply", threadRootMessageId: message.messageId, replyToMessageId: message.messageId },
  ]} />);
  const reply = screen.getByRole("button", { name: "Reply in thread, 1 reply" });
  await user.click(reply);
  expect(onSelectThread).toHaveBeenLastCalledWith("message");
  expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
  expect(screen.getByRole("region", { name: "Messages" })).toBeVisible();
  fireEvent.keyDown(screen.getByRole("complementary", { name: "Thread" }), { key: "Escape" });
  expect(onSelectThread).toHaveBeenLastCalledWith(null);
  expect(reply).toHaveFocus();
  expect(screen.queryByRole("complementary")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Show room details" })).toHaveAttribute("aria-expanded", "false");
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

it("keeps rendering a finished run's text instead of collapsing it", () => {
  const run: RoomRunView = { roomId: "room", agentId: "friday", runId: "run-1", text: "Partial answer", thinking: "", state: "completed", tools: [] };
  render(<RoomPanel {...props} runs={[run]} />);
  expect(within(screen.getByRole("article", { name: "Live run from Friday" })).getByText("Partial answer")).toBeInTheDocument();
  expect(screen.queryByText("Friday finished · no reply")).not.toBeInTheDocument();
});

it("lets wide room details change agent listen mode without giving humans a control", async () => {
  const user = userEvent.setup();
  const onListenModeChange = vi.fn();
  render(<RoomPanel {...props} onListenModeChange={onListenModeChange} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  const details = within(screen.getByRole("complementary", { name: "Room details" }));
  const mode = details.getByRole("combobox", { name: "Listen mode for Friday" });
  expect(mode).toHaveValue("mentions");
  expect(within(mode).getAllByRole("option").map((option) => option.textContent)).toEqual(["Active", "Mentions only", "Observe"]);
  expect(details.getAllByRole("combobox")).toHaveLength(1);
  expect(details.queryByRole("combobox", { name: "Listen mode for Owner" })).not.toBeInTheDocument();
  await user.selectOptions(mode, "observe");
  expect(onListenModeChange).toHaveBeenCalledWith("friday", "observe");
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

  await user.click(screen.getByRole("button", { name: "Show room details" }));
  await user.click(screen.getByRole("button", { name: "Add members" }));
  expect(screen.getByRole("checkbox", { name: "Colleague" })).toBeInTheDocument();
  expect(screen.getByRole("checkbox", { name: "Atlas" })).toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: "Owner" })).not.toBeInTheDocument();
  expect(screen.queryByRole("checkbox", { name: "Friday" })).not.toBeInTheDocument();

  await user.click(screen.getByRole("checkbox", { name: "Atlas" }));
  await user.click(screen.getByRole("button", { name: "Add selected" }));
  expect(onAddMember).toHaveBeenCalledWith({ kind: "agent", id: "atlas" });
});

it("shows member-add progress and a useful retryable error", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const onAddMember = vi.fn(() => new Promise<void>((_, fail) => { reject = fail; }));
  render(<RoomPanel {...props}
    room={{ ...props.room!, kind: "channel", name: "Launch" }}
    addMemberCandidates={[{ kind: "human", id: "colleague", displayName: "Colleague", mentionHandle: null }]}
    onAddMember={onAddMember} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  await user.click(screen.getByRole("button", { name: "Add members" }));
  await user.click(screen.getByRole("checkbox", { name: "Colleague" }));
  await user.click(screen.getByRole("button", { name: "Add selected" }));
  expect(screen.getByRole("button", { name: "Adding…" })).toBeDisabled();
  await act(async () => reject(new Error("You do not have permission to add members.")));
  expect(screen.getByRole("alert")).toHaveTextContent("You do not have permission to add members.");
  expect(screen.getByRole("button", { name: "Add selected" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Add members" }));
  await user.click(screen.getByRole("button", { name: "Add members" }));
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("resets member selection and picker state when the room changes", async () => {
  const user = userEvent.setup();
  const candidates = [{ kind: "agent" as const, id: "atlas", displayName: "Atlas", mentionHandle: null }];
  const view = render(<RoomPanel {...props} messages={[]} roomId="channel-a" room={{ ...props.room!, roomId: "channel-a", kind: "channel", name: "A" }}
    addMemberCandidates={candidates} onAddMember={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  await user.click(screen.getByRole("button", { name: "Add members" }));
  await user.click(screen.getByRole("checkbox", { name: "Atlas" }));
  expect(screen.getByRole("checkbox", { name: "Atlas" })).toBeChecked();

  view.rerender(<RoomPanel {...props} messages={[]} roomId="channel-b" room={{ ...props.room!, roomId: "channel-b", kind: "channel", name: "B" }}
    addMemberCandidates={candidates} onAddMember={vi.fn()} />);

  expect(screen.queryByRole("button", { name: "Add selected" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Show room details" }));
  await user.click(screen.getByRole("button", { name: "Add members" }));
  expect(screen.getByRole("checkbox", { name: "Atlas" })).not.toBeChecked();
});

it.each(["dm", "group-dm"] as const)("does not offer member adds in a %s", async (kind) => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind }}
    addMemberCandidates={[{ kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: null }]}
    onAddMember={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Show room details" }));
  expect(screen.queryByRole("button", { name: "Add members" })).not.toBeInTheDocument();
});

it("replaces the narrow conversation only while a thread is open", async () => {
  const user = userEvent.setup();
  const onLinkToWork = vi.fn();
  render(<RoomPanel {...props} narrow onLinkToWork={onLinkToWork} />);
  const history = screen.getByRole("region", { name: "Messages" });
  expect(history).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Link to work" }));
  expect(onLinkToWork).toHaveBeenCalledOnce();
  const trigger = screen.getByRole("button", { name: "Reply in thread" });
  await user.click(trigger);
  expect(history).not.toBeVisible();
  expect(screen.getByRole("heading", { name: "Thread" })).toHaveFocus();
  await user.click(screen.getByRole("button", { name: "Back to conversation" }));
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

it("collapses consecutive messages from the same author and reveals the time in the gutter", () => {
  const second: RoomMessage = { ...message, messageId: "message-2", messageSeq: 2, createdAt: "2026-09-10T00:01:00Z", text: "Again" };
  render(<RoomPanel {...props} messages={[message, second]} />);
  const articles = screen.getAllByRole("article", { name: "Message from Friday" });
  expect(articles).toHaveLength(2);
  expect(within(articles[0]).getByText("Friday")).toBeInTheDocument();
  expect(within(articles[1]).queryByText("Friday")).not.toBeInTheDocument();
  const time = new Date(second.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
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

  await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "First try");
  // Synchronous dispatch (no await before the assertion): the send is still in flight,
  // so a fix that waits for it to resolve must not have flipped sentSinceOpen yet.
  fireEvent.keyDown(screen.getByRole("textbox", { name: "Message" }), { key: "Enter" });
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  await waitFor(() => expect(screen.getByRole("textbox", { name: "Message" })).not.toBeDisabled());
  expect(onSubmit).toHaveBeenCalledTimes(1);
  // The failed send has now fully settled (composer re-enabled): the New line must
  // still be here, not just synchronously right after the keydown.
  expect(screen.getByRole("separator", { name: "New messages" })).toBeInTheDocument();

  await userEvent.type(screen.getByRole("textbox", { name: "Message" }), "Second try");
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
