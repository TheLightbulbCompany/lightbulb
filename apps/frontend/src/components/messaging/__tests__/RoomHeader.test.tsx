import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { RoomHeader } from "../RoomHeader";
import type { RoomParticipant, RoomSummary } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 9,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null,
};
const people: RoomParticipant[] = ["Aden", "Bo", "Cy", "Dee", "Eve"].map((name, index) => ({
  kind: "human", id: `u${index}`, displayName: name, mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
}));
const props = {
  room, roomName: "Roadmap", counterparts: people, participantCount: 6, canEditRoom: true,
  canLeave: true, actionable: true, pinnedCount: 2, detailsOpen: false, search: <span />,
  onOpenDetails: vi.fn(),
};

it("shows the topic and at most four faces with the remaining count", () => {
  render(<RoomHeader {...props} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "View members, 6 in this conversation" })).toHaveTextContent("+2");
});

it("edits the topic and saves it", async () => {
  const onSetTopic = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  const field = screen.getByLabelText("Topic");
  await userEvent.clear(field);
  await userEvent.type(field, "Ship v3{Enter}");
  await waitFor(() => expect(onSetTopic).toHaveBeenCalledWith("Ship v3"));
});

it("clears the topic when the field is emptied and cancels on Escape", async () => {
  const onSetTopic = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.clear(screen.getByLabelText("Topic"));
  await userEvent.keyboard("{Enter}");
  await waitFor(() => expect(onSetTopic).toHaveBeenCalledWith(null));

  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.type(screen.getByLabelText("Topic"), "nope{Escape}");
  expect(onSetTopic).toHaveBeenCalledTimes(1);
});

it("offers an add-topic affordance when there is none", () => {
  render(<RoomHeader {...props} room={{ ...room, topic: null }} onSetTopic={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Add a topic" })).toBeInTheDocument();
});

it("toggles star and mute and keeps a word beside the icon state", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onSetPrefs={onSetPrefs} />);
  await userEvent.click(screen.getByRole("button", { name: "Star this conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith({ starred: true });
  rerender(<RoomHeader {...props} onSetPrefs={onSetPrefs} room={{ ...room, starred: true, muted: true }} />);
  expect(screen.getByRole("button", { name: "Unstar this conversation" })).toHaveAttribute("aria-pressed", "true");
  await userEvent.click(screen.getByRole("button", { name: "Unmute this conversation" }));
  expect(onSetPrefs).toHaveBeenLastCalledWith({ muted: false });
});

it("renames from the overflow menu and never offers it on a DM", async () => {
  const onRenameRoom = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onRenameRoom={onRenameRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
  const dialog = await screen.findByRole("dialog", { name: "Rename conversation" });
  await userEvent.clear(within(dialog).getByLabelText("Conversation name"));
  await userEvent.type(within(dialog).getByLabelText("Conversation name"), "Roadmap 2");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save name" }));
  await waitFor(() => expect(onRenameRoom).toHaveBeenCalledWith("Roadmap 2"));

  rerender(<RoomHeader {...props} onRenameRoom={onRenameRoom} canEditRoom={false} room={{ ...room, kind: "dm", name: null }} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  expect(screen.queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
});

it("copies the room link and opens details from the overflow menu", async () => {
  const onOpenDetails = vi.fn();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<RoomHeader {...props} onOpenDetails={onOpenDetails} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("room=room-1"));
  await userEvent.click(screen.getByRole("button", { name: "Conversation options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Details" }));
  expect(onOpenDetails).toHaveBeenCalledWith();
});
