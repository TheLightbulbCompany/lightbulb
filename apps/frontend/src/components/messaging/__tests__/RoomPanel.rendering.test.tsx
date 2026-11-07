import { render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS, type RoomRunView } from "@lightbulb/shared";
import { RoomPanel, type RoomPanelProps } from "../RoomPanel";
import { composerValue } from "@/test-utils/composer";
import type { RoomMessage, RoomParticipant } from "../types";

vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));

const renders = vi.hoisted(() => vi.fn());
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

it("refreshes canonical content after history changes", () => {
  const view = render(<RoomPanel {...props} />);
  const updated = { ...message, text: "Edited reply" };
  view.rerender(<RoomPanel {...props} messages={[updated]} />);
  const article = screen.getByRole("article", { name: "Message from Agent" });
  expect(within(article).getByText("Edited reply")).toBeInTheDocument();
  const later = { ...message, messageId: "later", messageSeq: 3, text: "New canonical message" };
  view.rerender(<RoomPanel {...props} messages={[updated, later]} />);
  expect(screen.getByText("New canonical message")).toBeInTheDocument();
});
