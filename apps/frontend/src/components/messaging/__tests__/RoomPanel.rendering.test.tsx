import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS, type RoomRunView } from "@lightbulb/shared";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { composerValue } from "@/test-utils/composer";
import type { RoomMessage, RoomParticipant } from "../types";

vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));

const renders = vi.hoisted(() => vi.fn());
const bodyRenders = vi.hoisted(() => vi.fn());
vi.mock("react-markdown", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-markdown")>();
  return { ...original, default: (props: Parameters<typeof original.default>[0]) => {
    bodyRenders(props.children);
    return <original.default {...props} />;
  } };
});
vi.mock("../RoomMessageItem", async (importOriginal) => {
  const original = await importOriginal<typeof import("../RoomMessageItem")>();
  return { ...original, RoomMessageItem: (props: Parameters<typeof original.RoomMessageItem>[0]) => {
    renders(props.message.messageId);
    return <original.RoomMessageItem {...props} />;
  } };
});

const owner: RoomParticipant = { kind: "human", id: "owner", displayName: "Owner", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
const agent: RoomParticipant = { ...owner, kind: "agent", id: "agent", displayName: "Agent", role: "member" };
const message: RoomMessage = { messageId: "message", roomId: "room", messageSeq: 1, roomRevision: 1, author: { kind: "agent", id: "agent", displayName: "Agent" }, text: "Original reply", mentions: [], replyToMessageId: null, threadRootMessageId: null, createdAt: "2026-09-10T00:00:00Z", runId: null };
const props: RoomPanelProps = {
  ownerScopeId: "org", currentUserId: "owner", roomId: "room", state: "ready", httpReady: true, liveConnected: true,
  room: { roomId: "room", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: message.createdAt, topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0 },
  participants: [owner, agent], messages: [message],
  composer: { text: "", onTextChange: vi.fn(), members: [owner, agent], clientMessageId: "draft", sendState: "idle", onSubmit: vi.fn() },
};

it("keeps canonical history out of draft and live-run renders", () => {
  const run: RoomRunView = { roomId: "room", agentId: "agent", runId: "run", text: "Starting work", thinking: "", state: "running", tools: [] };
  const view = render(<RoomPanel {...props} runs={[run]} />);
  renders.mockClear();
  view.rerender(<RoomPanel {...props} runs={[run]} composer={{ ...props.composer, text: "my draft" }} />);
  expect(composerValue()).toBe("my draft");
  expect(renders).not.toHaveBeenCalled();

  view.rerender(<RoomPanel {...props} runs={[{ ...run, text: "Work progressed" }]} composer={{ ...props.composer, text: "my draft" }} />);
  expect(screen.getByText("Work progressed")).toBeInTheDocument();
  expect(screen.queryByText("Starting work")).not.toBeInTheDocument();
  expect(renders).not.toHaveBeenCalled();
});

it("keeps an ended run's background subagents under the saved reply that replaced its card", () => {
  const subagents = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, "agent", "subagent.complete",
    { subagent_id: "sa-1", goal: "Survey pricing", delegation_id: "d-1", status: "completed" }, 1000);
  const delegationsByMessage = new Map([["message", { agentId: "agent", delegationIds: ["d-1", "d-gone"] }]]);
  render(<RoomPanel {...props} subagents={subagents} delegationsByMessage={delegationsByMessage} />);
  expect(screen.getByText("Survey pricing")).toBeInTheDocument();
  expect(screen.getByLabelText("Completed")).toBeInTheDocument();
  // A delegation the reducer no longer holds still says it was dispatched.
  expect(screen.getByText("Background subagents dispatched")).toBeInTheDocument();
});

it("keeps a thread run's background subagents under its saved reply in the thread pane", () => {
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "A thread reply", threadRootMessageId: message.messageId, createdAt: "2026-09-11T00:00:00Z" };
  const delegationsByMessage = new Map([["reply", { agentId: "agent", delegationIds: ["d-1"] }]]);
  render(<RoomPanel {...props} messages={[message, reply]} subagents={EMPTY_ROOM_SUBAGENTS} delegationsByMessage={delegationsByMessage} />);
  fireEvent.click(screen.getByRole("button", { name: /Reply in thread, 1 reply/ }));
  expect(within(screen.getByRole("complementary", { name: "Thread" })).getByText("Background subagents dispatched")).toBeInTheDocument();
});

it("renders a session mention as a reference card with no delivery state", () => {
  const tagged: RoomMessage = { ...message, text: "Please review this", sessionMentions: ["s1"],
    sessionTargetLabels: { s1: "Claude Code · Launch check · Owner" } };
  const view = render(<RoomPanel {...props} messages={[tagged]} />);
  expect(screen.getByText("Please review this")).toBeInTheDocument();
  expect(screen.getByText("Claude Code · Launch check")).toBeInTheDocument();
  // Not in the viewer's list (e.g. ended over a week ago): no status, but Open still routes by id.
  expect(screen.queryByText("Unavailable")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open session Claude Code · Launch check" })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  // The card resolves from every session the viewer can open, not the composer's sendable targets:
  // an ended teammate session still opens.
  view.rerender(<RoomPanel {...props} messages={[tagged]} sessions={[
    { sessionId: "s1", provider: "claude", title: "Launch check", status: "ended" } as never,
  ]} />);
  expect(screen.getByRole("button", { name: "Open session Claude · Launch check" })).toBeInTheDocument();
});

it("refreshes canonical content, reactions and thread metadata after history changes", () => {
  const onSetReaction = vi.fn(async () => {});
  const view = render(<RoomPanel {...props} onSetReaction={onSetReaction} />);
  const updated = { ...message, text: "Edited reply", reactions: { "👍": ["owner"] } };
  const reply = { ...message, messageId: "reply", messageSeq: 2, text: "A thread reply", threadRootMessageId: message.messageId, createdAt: "2026-09-11T00:00:00Z" };
  view.rerender(<RoomPanel {...props} messages={[updated, reply]} onSetReaction={onSetReaction} />);
  const article = screen.getByRole("article", { name: "Message from Agent" });
  expect(within(article).getByText("Edited reply")).toBeInTheDocument();
  expect(within(article).getByRole("button", { name: "Remove 👍 reaction, 1" })).toBeInTheDocument();
  const later = { ...message, messageId: "later", messageSeq: 3, text: "New canonical message" };
  view.rerender(<RoomPanel {...props} messages={[updated, reply, later]} onSetReaction={onSetReaction} />);
  expect(screen.getByText("New canonical message")).toBeInTheDocument();
  const thread = within(article).getByRole("button", { name: /Reply in thread, 1 reply · last reply/ });
  fireEvent.click(thread);
  expect(within(screen.getByRole("complementary", { name: "Thread" })).getByText("A thread reply")).toBeInTheDocument();
});

it("uses replacement callbacks when a message action is clicked", () => {
  const oldSelect = vi.fn();
  const newSelect = vi.fn();
  const view = render(<RoomPanel {...props} onSelectThread={oldSelect} />);
  view.rerender(<RoomPanel {...props} onSelectThread={newSelect} />);
  fireEvent.click(screen.getByRole("button", { name: "Reply in thread" }));
  expect(newSelect).toHaveBeenCalledWith(message.messageId);
  expect(oldSelect).not.toHaveBeenCalled();
});

it("updates thread working membership without rerendering history for streamed tokens", () => {
  const run: RoomRunView = { roomId: "room", agentId: "agent", runId: "run", threadRootMessageId: message.messageId, text: "", thinking: "", state: "running", tools: [] };
  const view = render(<RoomPanel {...props} runs={[run]} />);
  expect(screen.getByRole("button", { name: "Reply in thread, Agent is working…" })).toBeInTheDocument();
  renders.mockClear();
  view.rerender(<RoomPanel {...props} runs={[{ ...run, text: "Progress" }]} />);
  expect(renders).not.toHaveBeenCalled();
  view.rerender(<RoomPanel {...props} runs={[{ ...run, state: "completed" }]} />);
  expect(screen.queryByRole("button", { name: /Agent is working/ })).not.toBeInTheDocument();
});

it("preserves the room-wide reaction lock and failure feedback without rerendering message bodies", async () => {
  let reject!: (error: Error) => void;
  const onSetReaction = vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail; }));
  const second = { ...message, messageId: "second", messageSeq: 2, text: "Second reply", reactions: { "👍": ["owner"] } };
  render(<RoomPanel {...props} messages={[message, second]} onSetReaction={onSetReaction} />);
  expect(bodyRenders.mock.calls.some(([text]) => text === message.text)).toBe(true);
  expect(bodyRenders.mock.calls.some(([text]) => text === second.text)).toBe(true);
  bodyRenders.mockClear();
  const articles = screen.getAllByRole("article", { name: "Message from Agent" });
  fireEvent.click(within(articles[0]).getByRole("button", { name: "React 👍" }));
  expect(onSetReaction).toHaveBeenCalledExactlyOnceWith(message.messageId, "👍", true);
  for (const article of articles) {
    expect(within(article).getByRole("button", { name: "React 👍" })).toBeDisabled();
    expect(within(article).getByRole("button", { name: "Add reaction" })).toBeDisabled();
    expect(within(article).getByRole("button", { name: "Reply in thread" })).toBeEnabled();
  }
  const existing = within(articles[1]).getByRole("button", { name: "Remove 👍 reaction, 1" });
  expect(existing).toBeDisabled();
  fireEvent.click(existing);
  expect(onSetReaction).toHaveBeenCalledTimes(1);
  expect(bodyRenders).not.toHaveBeenCalled();

  await act(async () => reject(new Error("Unavailable")));
  const error = within(articles[0]).getByRole("alert");
  expect(error).toHaveTextContent("Could not update reaction");
  expect(error.closest(".room-message-actions")).toBeNull();
  expect(within(articles[1]).queryByRole("alert")).not.toBeInTheDocument();
  expect(existing).toBeEnabled();
  for (const article of articles) expect(within(article).getByRole("button", { name: "Add reaction" })).toBeEnabled();
  expect(bodyRenders).not.toHaveBeenCalled();
});
