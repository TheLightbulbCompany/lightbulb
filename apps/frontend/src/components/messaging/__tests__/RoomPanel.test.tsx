import { fireEvent, render, screen, within } from "@testing-library/react";
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

it("excludes only the signed-in human when naming a group DM", () => {
  render(<RoomPanel {...props} room={{ ...props.room!, kind: "group-dm" }} participants={[owner, { ...owner, id: "alice", displayName: "Alice" }, { ...friday, id: "owner", displayName: "Atlas" }]} messages={[]} />);
  expect(screen.getByRole("heading", { name: "Alice and Atlas" })).toBeInTheDocument();
  expect(screen.getByRole("region", { name: "Alice and Atlas" })).toBeInTheDocument();
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
  const details = screen.getByRole("complementary", { name: "Room details" });
  expect(within(details).getByText("Friday")).toBeInTheDocument();
  expect(within(details).getByText("Owner")).toBeInTheDocument();
  expect(within(details).getByText("Agent")).toBeInTheDocument();
  expect(within(details).getByText("Person")).toBeInTheDocument();
  expect(within(details).getAllByText("Friday is working")).toHaveLength(1);
  const toggle = screen.getByRole("button", { name: "Hide room details" });
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
  render(<RoomPanel {...props} onSelectThread={onSelectThread} />);
  await user.click(screen.getByRole("button", { name: "Hide room details" }));
  const reply = screen.getByRole("button", { name: "Reply" });
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

it("lets wide room details change agent listen mode without giving humans a control", async () => {
  const user = userEvent.setup();
  const onListenModeChange = vi.fn();
  render(<RoomPanel {...props} onListenModeChange={onListenModeChange} />);
  const details = within(screen.getByRole("complementary", { name: "Room details" }));
  const mode = details.getByRole("combobox", { name: "Listen mode for Friday" });
  expect(mode).toHaveValue("mentions");
  expect(within(mode).getAllByRole("option").map((option) => option.textContent)).toEqual(["Active", "Mentions only", "Observe"]);
  expect(details.getAllByRole("combobox")).toHaveLength(1);
  expect(details.queryByRole("combobox", { name: "Listen mode for Owner" })).not.toBeInTheDocument();
  await user.selectOptions(mode, "observe");
  expect(onListenModeChange).toHaveBeenCalledWith("friday", "observe");
});

it.each(["thread", "context"] as const)("replaces the narrow conversation only while selected %s is open", async (kind) => {
  const user = userEvent.setup();
  const onLinkToWork = vi.fn();
  render(<RoomPanel {...props} narrow onLinkToWork={onLinkToWork}
    messages={[{ ...message, contextIds: ["approval"] }]}
    contexts={[{ id: "approval", roomId: "room", kind: "approval", title: "Review proposal", content: "Proposal details" }]} />);
  const history = screen.getByRole("region", { name: "Messages" });
  expect(history).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Link to work" }));
  expect(onLinkToWork).toHaveBeenCalledOnce();
  const trigger = screen.getByRole("button", { name: kind === "thread" ? "Reply" : "Review proposal" });
  await user.click(trigger);
  expect(history).not.toBeVisible();
  expect(screen.getByRole("heading", { name: kind === "thread" ? "Thread" : "Review proposal" })).toHaveFocus();
  if (kind === "context") expect(screen.getByText("Proposal details")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Back to conversation" }));
  expect(history).toBeVisible();
  expect(trigger).toHaveFocus();
});
