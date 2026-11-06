import { readFileSync } from "node:fs";
import path from "node:path";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
import { RoomHeader } from "../RoomHeader";
import type { RoomSummary } from "@lightbulb/shared";
import type { RoomParticipant } from "../types";

const room: RoomSummary = {
  roomId: "room-1", name: "Roadmap", kind: "channel", roomRevision: 3, lastMessageSeq: 9,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-11T10:00:00Z",
  topic: "Ship v2", starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
};
const people: RoomParticipant[] = ["Aden", "Bo", "Cy", "Dee", "Eve"].map((name, index) => ({
  kind: "human", id: `u${index}`, displayName: name, mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "2026-09-01T00:00:00Z", joinedSeq: 0,
}));
const props = {
  room, roomName: "Roadmap", counterparts: people, participantCount: 6, canEditRoom: true,
  canLeave: true, actionable: true, pinnedCount: 2, search: <span />,
  onOpenDetails: vi.fn(),
};

it("shows the topic and at most four faces with the remaining count", () => {
  render(<RoomHeader {...props} />);
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "People, 6 here" })).toHaveTextContent("+2");
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

it("caps the topic at the shared 2,000-character Instructions limit", async () => {
  render(<RoomHeader {...props} onSetTopic={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  expect(screen.getByLabelText("Topic")).toHaveAttribute("maxLength", "2000");
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

it("clears a failed topic save's banner when the edit is cancelled with Escape", async () => {
  const onSetTopic = vi.fn().mockRejectedValue(new Error("Topic rejected"));
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.type(screen.getByLabelText("Topic"), " again{Enter}");
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Topic rejected"));

  await userEvent.type(screen.getByLabelText("Topic"), "{Escape}");

  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("saves the topic from the Save button, showing a pending label", async () => {
  let resolve!: () => void;
  const onSetTopic = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  await userEvent.click(screen.getByRole("button", { name: "Edit topic" }));
  await userEvent.clear(screen.getByLabelText("Topic"));
  await userEvent.type(screen.getByLabelText("Topic"), "Ship v3");
  await userEvent.click(screen.getByRole("button", { name: "Save" }));
  expect(onSetTopic).toHaveBeenCalledWith("Ship v3");
  expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
  await act(async () => resolve());
  expect(screen.queryByLabelText("Topic")).not.toBeInTheDocument();
});

it("cancels the topic edit from the Cancel button, the same as Escape", async () => {
  const onSetTopic = vi.fn();
  render(<RoomHeader {...props} onSetTopic={onSetTopic} />);
  const trigger = screen.getByRole("button", { name: "Edit topic" });
  await userEvent.click(trigger);
  await userEvent.clear(screen.getByLabelText("Topic"));
  await userEvent.type(screen.getByLabelText("Topic"), "nope");
  await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
  expect(onSetTopic).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("Topic")).not.toBeInTheDocument();
  expect(screen.getByText("Ship v2")).toBeInTheDocument();
});

it("offers an add-topic affordance when there is none", () => {
  render(<RoomHeader {...props} room={{ ...room, topic: null }} onSetTopic={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Add a topic" })).toBeInTheDocument();
});

it("toggles star and mute from the overflow menu, naming the current state in words", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onSetPrefs={onSetPrefs} />);
  // Star and mute live in the ⋯ menu now, not as header buttons.
  expect(screen.queryByRole("button", { name: /^star$/i })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^mute$/i })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star" }));
  expect(onSetPrefs).toHaveBeenCalledWith({ starred: true });
  rerender(<RoomHeader {...props} onSetPrefs={onSetPrefs} room={{ ...room, starred: true, muted: true }} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  expect(await screen.findByRole("menuitem", { name: "Unstar" })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("menuitem", { name: "Unmute" }));
  expect(onSetPrefs).toHaveBeenLastCalledWith({ muted: false });
});

it("renames from the overflow menu and never offers it on a DM", async () => {
  const onRenameRoom = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onRenameRoom={onRenameRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
  const dialog = await screen.findByRole("dialog", { name: "Rename channel" });
  await userEvent.clear(within(dialog).getByLabelText("Name"));
  await userEvent.type(within(dialog).getByLabelText("Name"), "Roadmap 2");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save name" }));
  await waitFor(() => expect(onRenameRoom).toHaveBeenCalledWith("Roadmap 2"));

  rerender(<RoomHeader {...props} onRenameRoom={onRenameRoom} canEditRoom={false} room={{ ...room, kind: "dm", name: null }} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  expect(screen.queryByRole("menuitem", { name: "Rename" })).not.toBeInTheDocument();
});

it("copies the room link and opens details from the overflow menu", async () => {
  const onOpenDetails = vi.fn();
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<RoomHeader {...props} onOpenDetails={onOpenDetails} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Copy link" }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("room=room-1"));
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Details" }));
  expect(onOpenDetails).toHaveBeenCalledWith("about");
});

it("does not leak a failed star's error into a freshly opened rename dialog", async () => {
  const onSetPrefs = vi.fn().mockRejectedValueOnce(new Error("Could not star."));
  const onRenameRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onSetPrefs={onSetPrefs} onRenameRoom={onRenameRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not star.");
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Rename" }));
  const dialog = await screen.findByRole("dialog", { name: "Rename channel" });
  expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
});

it("opens the pins tab of the details panel from the pinned count", async () => {
  const onOpenDetails = vi.fn();
  render(<RoomHeader {...props} onOpenDetails={onOpenDetails} />);
  const pins = screen.getByRole("button", { name: "2 pinned messages" });
  // No aria-expanded: this only ever opens the panel (and may open it on a
  // different tab than the one already showing), so it is not a disclosure toggle.
  expect(pins).not.toHaveAttribute("aria-expanded");
  await userEvent.click(pins);
  expect(onOpenDetails).toHaveBeenCalledWith("pins");
});

it("hides the pinned count when nothing is pinned", () => {
  render(<RoomHeader {...props} pinnedCount={0} />);
  expect(screen.queryByRole("button", { name: /pinned/ })).not.toBeInTheDocument();
});

// Options and the view switches are 32-36px square and Search is 42px wide
// (h-9 px-4) -- all under the 44px touch floor at <=859px. jsdom has no `css:
// true` here, so the media-query rule can't be observed via computed style;
// assert the DOM structure the selector needs (the buttons live inside
// .room-audience) plus the rule's source text instead.
it("puts About, Conversation options and Search inside .room-audience for the mobile touch-floor rule", () => {
  // The default `search` prop is a bare <span /> -- stand in a real Search
  // trigger (same accessible name RoomMessageSearch renders) so the element
  // the mobile audit flagged is actually exercised here too.
  render(<RoomHeader {...props} onSetPrefs={vi.fn()} search={<button type="button" aria-label="Search this chat">Search</button>}
    views={<button type="button">About</button>} />);
  const audience = screen.getByRole("button", { name: "Options" }).closest(".room-audience");
  expect(audience).not.toBeNull();
  expect(within(audience as HTMLElement).getByRole("button", { name: "About" })).toBeInTheDocument();
  expect(within(audience as HTMLElement).getByRole("button", { name: "Search this chat" })).toBeInTheDocument();

  const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
  expect(css).toMatch(/@media \(max-width: 859px\) \{[\s\S]*\.room-audience button \{ min-width: 44px; min-height: 44px; \}/);
});

it("marks a shared channel", () => {
  render(<RoomHeader {...props} room={{ ...room, shared: true }} />);
  expect(screen.getByText("Shared")).toHaveAttribute("title", "Shared between this workspace and Lightbulb");
});

it("archives a channel only after confirming, and unarchives an archived one directly", async () => {
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  const { rerender } = render(<RoomHeader {...props} onSetArchived={onSetArchived} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Archive channel" }));
  const dialog = screen.getByRole("dialog", { name: "Archive Roadmap?" });
  expect(onSetArchived).not.toHaveBeenCalled();
  await userEvent.click(within(dialog).getByRole("button", { name: "Archive channel" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(true));

  rerender(<RoomHeader {...props} room={{ ...room, archivedAt: "2026-09-23T00:00:00Z" }} onSetArchived={onSetArchived} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Unarchive channel" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(false));
});

it.each([{ isDefault: true }, { shared: true }, { kind: "dm" as const }, { canEditRoom: false }])("offers no archive for %o", async (extra) => {
  const { canEditRoom = true, ...roomExtra } = extra as { canEditRoom?: boolean };
  render(<RoomHeader {...props} canEditRoom={canEditRoom} room={{ ...room, ...roomExtra }} onSetArchived={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  expect(screen.queryByRole("menuitem", { name: /Archive/ })).not.toBeInTheDocument();
});

it("lets any member archive and leave a group chat", async () => {
  const onSetArchived = vi.fn().mockResolvedValue(undefined);
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} canEditRoom={false} room={{ ...room, kind: "group-dm" }} onSetArchived={onSetArchived} onLeaveRoom={onLeaveRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Archive group chat" }));
  await userEvent.click(within(screen.getByRole("dialog", { name: "Archive Roadmap?" })).getByRole("button", { name: "Archive group chat" }));
  await waitFor(() => expect(onSetArchived).toHaveBeenCalledWith(true));

  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Leave" }));
  await userEvent.click(within(screen.getByRole("dialog", { name: "Leave Roadmap?" })).getByRole("button", { name: "Leave group chat" }));
  await waitFor(() => expect(onLeaveRoom).toHaveBeenCalled());
});

it("deletes a channel only once its name is typed exactly", async () => {
  const onDeleteRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomHeader {...props} onDeleteRoom={onDeleteRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Delete channel" }));
  const dialog = screen.getByRole("dialog", { name: "Delete Roadmap?" });
  const confirm = within(dialog).getByRole("button", { name: "Delete channel" });
  const field = within(dialog).getByLabelText(/to confirm/);
  expect(confirm).toBeDisabled();
  await userEvent.type(field, "roadmap{Enter}");
  expect(confirm).toBeDisabled();
  expect(onDeleteRoom).not.toHaveBeenCalled();
  await userEvent.clear(field);
  await userEvent.type(field, "Roadmap");
  expect(confirm).toBeEnabled();
  await userEvent.click(confirm);
  await waitFor(() => expect(onDeleteRoom).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

it.each([{}, { isDefault: true }, { shared: true }, { kind: "group-dm" as const }, { kind: "dm" as const }])("offers no delete for %o without an owner handler or on a protected room", async (extra) => {
  const onDeleteRoom = Object.keys(extra).length ? vi.fn() : undefined;
  render(<RoomHeader {...props} room={{ ...room, ...extra }} onDeleteRoom={onDeleteRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  expect(screen.queryByRole("menuitem", { name: "Delete channel" })).not.toBeInTheDocument();
});

it("explains ownership instead of offering a Leave that would fail, and offers Delete in its place", async () => {
  const onDeleteRoom = vi.fn().mockResolvedValue(undefined);
  // Mirrors production wiring for a channel owner: no onLeaveRoom, canLeave
  // false, ownerCannotLeave true, onDeleteRoom present (the owner may delete).
  render(<RoomHeader {...props} onLeaveRoom={undefined} canLeave={false} ownerCannotLeave onDeleteRoom={onDeleteRoom} />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Leave" }));
  const dialog = screen.getByRole("dialog", { name: "Can't leave Roadmap" });
  expect(within(dialog).getByText("You are the only person in this channel. Delete it instead.")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: /^Leave/ })).not.toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete channel" }));
  const deleteDialog = screen.getByRole("dialog", { name: "Delete Roadmap?" });
  await userEvent.type(within(deleteDialog).getByLabelText(/to confirm/), "Roadmap");
  await userEvent.click(within(deleteDialog).getByRole("button", { name: "Delete channel" }));
  await waitFor(() => expect(onDeleteRoom).toHaveBeenCalledTimes(1));
});

it("explains ownership with no Delete offer when the owner may not delete the room", async () => {
  render(<RoomHeader {...props} onLeaveRoom={undefined} canLeave={false} ownerCannotLeave />);
  await userEvent.click(screen.getByRole("button", { name: "Options" }));
  await userEvent.click(screen.getByRole("menuitem", { name: "Leave" }));
  const dialog = screen.getByRole("dialog", { name: "Can't leave Roadmap" });
  expect(within(dialog).getByText("You are the only person in this channel. Delete it instead.")).toBeInTheDocument();
  expect(within(dialog).queryByRole("button", { name: "Delete channel" })).not.toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Cancel" })).toBeInTheDocument();
});
