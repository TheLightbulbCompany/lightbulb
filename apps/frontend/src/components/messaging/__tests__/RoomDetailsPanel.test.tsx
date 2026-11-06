import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomDetailsPanel, extractLinks } from "../RoomDetailsPanel";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomMessage } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 4,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
};
const message = (id: string, extra: Partial<RoomMessage> = {}): RoomMessage => ({
  messageId: id, roomId: "room-1", messageSeq: Number(id.slice(1)), roomRevision: 1,
  author: { kind: "agent", id: "friday", displayName: "Friday" }, text: `body ${id}`,
  mentions: [], replyToMessageId: null, threadRootMessageId: null,
  createdAt: new Date(2026, 8, 11, 10, 0).toISOString(), runId: null, ...extra,
});
const props = {
  room, roomName: "Roadmap", tab: "pins" as const, onSelectTab: vi.fn(), participants: [],
  actionable: true, pinnedMessageIds: [], activeRuns: [],
  onOpenMessage: vi.fn(), members: <p>Members go here</p>, messages: [] as RoomMessage[],
};

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date(2026, 8, 11, 12, 0)); });
afterEach(() => { vi.useRealTimers(); });

it("lists pinned messages and unpins one", async () => {
  const onUnpin = vi.fn().mockResolvedValue(undefined);
  render(<RoomDetailsPanel {...props} messages={[message("m1"), message("m2")]} pinnedMessageIds={["m2"]} onUnpin={onUnpin} />);
  const pins = screen.getByRole("list", { name: "Pinned messages" });
  expect(within(pins).getAllByRole("listitem")).toHaveLength(1);
  expect(within(pins).getByText(/body m2/)).toBeInTheDocument();
  await userEvent.click(within(pins).getByRole("button", { name: "Unpin this message" }));
  expect(onUnpin).toHaveBeenCalledWith("m2");
});

it("keeps a pinned message that is not loaded yet reachable", async () => {
  const onOpenMessage = vi.fn();
  render(<RoomDetailsPanel {...props} messages={[]} pinnedMessageIds={["gone"]} onOpenMessage={onOpenMessage} />);
  expect(screen.getByText("Message not loaded yet")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Jump to message" }));
  expect(onOpenMessage).toHaveBeenCalledWith("gone");
});

it("shows an empty state per tab, scoping files/links to loaded history rather than claiming the room has none", () => {
  const { rerender } = render(<RoomDetailsPanel {...props} />);
  expect(screen.getByText("No pinned messages yet.")).toBeInTheDocument();
  rerender(<RoomDetailsPanel {...props} tab="files" />);
  expect(screen.getByText("No files in the messages loaded so far. Scroll back in the chat to load more.")).toBeInTheDocument();
  rerender(<RoomDetailsPanel {...props} tab="links" />);
  expect(screen.getByText("No links in the messages loaded so far. Scroll back in the chat to load more.")).toBeInTheDocument();
});

it("lists messages carrying attachments under Files", () => {
  render(<RoomDetailsPanel {...props} tab="files"
    messages={[message("m1"), message("m2", { attachments: [{ id: "a1", fileName: "plan.png", contentType: "image/png", size: 10 }] })]} />);
  const files = screen.getByRole("list", { name: "Shared files" });
  expect(within(files).getAllByRole("listitem")).toHaveLength(1);
  expect(within(files).getByText("plan.png")).toBeInTheDocument();
});

it("lists shared links and leaves the rest of history alone", () => {
  render(<RoomDetailsPanel {...props} tab="links" messages={[message("m1", { text: "see https://example.com/a" })]} />);
  const links = screen.getByRole("list", { name: "Shared links" });
  const link = within(links).getByRole("link", { name: "example.com/a" });
  expect(link).toHaveAttribute("href", "https://example.com/a");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
});

it("shows the topic, created date and member count under About", () => {
  render(<RoomDetailsPanel {...props} tab="about" participants={[]} room={{ ...room }} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByText("0 members")).toBeInTheDocument();
});

it("exposes the five room tabs (no Requests tab without threads) as a tablist and moves between them with the arrow keys", async () => {
  const onSelectTab = vi.fn();
  render(<RoomDetailsPanel {...props} tab="members" onSelectTab={onSelectTab} />);
  const tabs = screen.getByRole("tablist", { name: "Room details" });
  expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Members", "Pins", "Files", "Links", "Details"]);
  const members = within(tabs).getByRole("tab", { name: "Members" });
  expect(members).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tabpanel", { name: "Members" })).toHaveTextContent("Members go here");
  members.focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(onSelectTab).toHaveBeenLastCalledWith("pins");
  await userEvent.keyboard("{ArrowLeft}");
  expect(onSelectTab).toHaveBeenLastCalledWith("about");
  onSelectTab.mockClear();
  await userEvent.keyboard("{End}");
  expect(onSelectTab).toHaveBeenCalledExactlyOnceWith("about");
  onSelectTab.mockClear();
  await userEvent.keyboard("{Home}");
  expect(onSelectTab).toHaveBeenCalledExactlyOnceWith("members");
  await userEvent.click(within(tabs).getByRole("tab", { name: "Files" }));
  expect(onSelectTab).toHaveBeenLastCalledWith("files");
});

it("extracts, dedupes and orders links newest first", () => {
  const links = extractLinks([
    message("m1", { text: "see https://example.com/a and https://example.com/a again" }),
    message("m2", { text: "then https://example.com/b", createdAt: new Date(2026, 8, 11, 11, 0).toISOString() }),
    message("m3", { text: "javascript:alert(1) is not a link", deletedAt: null }),
    message("m4", { text: "https://example.com/c", deletedAt: new Date(2026, 8, 11, 11, 30).toISOString() }),
  ]);
  expect(links.map((link) => link.href)).toEqual(["https://example.com/b", "https://example.com/a"]);
});

it("confirms before leaving the room from About", async () => {
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomDetailsPanel {...props} tab="about" onLeaveRoom={onLeaveRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Leave channel" }));
  expect(onLeaveRoom).not.toHaveBeenCalled();
  expect(screen.getByRole("alert")).toHaveTextContent("Leave Roadmap? You will lose access to this conversation and its messages.");
  expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  await userEvent.click(screen.getByRole("button", { name: "Leave channel" }));
  expect(onLeaveRoom).toHaveBeenCalled();
});

it("explains ownership under About instead of offering a Leave button, when no onLeaveRoom is given", () => {
  render(<RoomDetailsPanel {...props} tab="about" onLeaveRoom={undefined} ownerCannotLeave />);
  expect(screen.queryByRole("button", { name: "Leave channel" })).not.toBeInTheDocument();
  expect(screen.getByText("You are the only person in this channel. Delete it from the conversation options menu.")).toBeInTheDocument();
});

it("keeps a deleted pinned message unpinnable without showing its text", async () => {
  const onUnpin = vi.fn().mockResolvedValue(undefined);
  render(<RoomDetailsPanel {...props} onUnpin={onUnpin} pinnedMessageIds={["m1"]}
    messages={[message("m1", { deletedAt: new Date(2026, 8, 11, 11, 0).toISOString() })]} />);
  expect(screen.getByText("Message deleted")).toBeInTheDocument();
  expect(screen.queryByText(/body m1/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Unpin this message" }));
  expect(onUnpin).toHaveBeenCalledWith("m1");
});

it("names each jump for its own row and marks only the row being unpinned busy", async () => {
  let finish!: () => void;
  const onUnpin = vi.fn(() => new Promise<void>((done) => { finish = done; }));
  render(<RoomDetailsPanel {...props} messages={[message("m1"), message("m2")]} pinnedMessageIds={["m1", "m2"]} onUnpin={onUnpin} />);
  const jumps = screen.getAllByRole("button", { name: /^Jump to message from Friday at / });
  expect(jumps).toHaveLength(2);
  await userEvent.click(screen.getAllByRole("button", { name: "Unpin this message" })[0]);
  expect(onUnpin).toHaveBeenCalledWith("m2");
  expect(screen.getByRole("button", { name: "Unpinning…" })).toHaveAttribute("aria-busy", "true");
  const other = screen.getByRole("button", { name: "Unpin this message" });
  expect(other).not.toHaveAttribute("aria-busy", "true");
  expect(other).toBeDisabled();
  await act(async () => finish());
  expect(screen.getAllByRole("button", { name: "Unpin this message" })).toHaveLength(2);
});

it("returns focus to the Pins tab after a successful unpin, rather than dropping it to <body>", async () => {
  const onUnpin = vi.fn().mockResolvedValue(undefined);
  render(<RoomDetailsPanel {...props} messages={[message("m1")]} pinnedMessageIds={["m1"]} onUnpin={onUnpin} />);
  await userEvent.click(screen.getByRole("button", { name: "Unpin this message" }));
  expect(screen.getByRole("tab", { name: "Pins" })).toHaveFocus();
});

it("reports a failed unpin and leaves the row usable", async () => {
  const onUnpin = vi.fn().mockRejectedValue(new Error("A room can pin at most 50 messages."));
  render(<RoomDetailsPanel {...props} messages={[message("m1")]} pinnedMessageIds={["m1"]} onUnpin={onUnpin} />);
  await userEvent.click(screen.getByRole("button", { name: "Unpin this message" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("A room can pin at most 50 messages.");
  expect(screen.getByRole("button", { name: "Unpin this message" })).toBeEnabled();
});

it("leads with a Requests tab when the room's requests are given, and shows them", () => {
  render(<RoomDetailsPanel {...props} tab="threads" threads={<p>Room requests</p>} />);
  const tabs = screen.getByRole("tablist", { name: "Room details" });
  expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Tasks", "Members", "Pins", "Files", "Links", "Details"]);
  expect(screen.getByRole("tabpanel", { name: "Tasks" })).toHaveTextContent("Room requests");
});

