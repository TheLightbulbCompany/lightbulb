import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { RoomDetailsPanel } from "../RoomDetailsPanel";
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
  room, roomName: "Roadmap", tab: "files" as const, onSelectTab: vi.fn(), participants: [],
  actionable: true, activeRuns: [],
  onOpenMessage: vi.fn(), members: <p>Members go here</p>, messages: [] as RoomMessage[],
};

beforeEach(() => { vi.useFakeTimers({ toFake: ["Date"] }); vi.setSystemTime(new Date(2026, 8, 11, 12, 0)); });
afterEach(() => { vi.useRealTimers(); });

it("shows an empty Files state scoped to loaded history rather than claiming the room has none", () => {
  render(<RoomDetailsPanel {...props} tab="files" />);
  expect(screen.getByText("No files in the messages loaded so far. Scroll back in the chat to load more.")).toBeInTheDocument();
});

it("lists messages carrying attachments under Files", () => {
  render(<RoomDetailsPanel {...props} tab="files"
    messages={[message("m1"), message("m2", { attachments: [{ id: "a1", fileName: "plan.png", contentType: "image/png", size: 10 }] })]} />);
  const files = screen.getByRole("list", { name: "Shared files" });
  expect(within(files).getAllByRole("listitem")).toHaveLength(1);
  expect(within(files).getByText("plan.png")).toBeInTheDocument();
});

it("shows the topic, created date and member count under About", () => {
  render(<RoomDetailsPanel {...props} tab="about" participants={[]} room={{ ...room }} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByText("0 members")).toBeInTheDocument();
});

it("exposes the three room tabs as a tablist and moves between them with the arrow keys", async () => {
  const onSelectTab = vi.fn();
  render(<RoomDetailsPanel {...props} tab="members" onSelectTab={onSelectTab} />);
  const tabs = screen.getByRole("tablist", { name: "Room details" });
  expect(within(tabs).getAllByRole("tab").map((tab) => tab.textContent)).toEqual(["Members", "Files", "Details"]);
  const members = within(tabs).getByRole("tab", { name: "Members" });
  expect(members).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tabpanel", { name: "Members" })).toHaveTextContent("Members go here");
  members.focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(onSelectTab).toHaveBeenLastCalledWith("files");
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

