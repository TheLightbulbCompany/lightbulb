import { readFileSync } from "node:fs";
import path from "node:path";
import { act, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { RoomDirectory } from "../RoomDirectory";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const friday: DirectoryParticipant = { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: null, agentActivity: "running" };
const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const atlas: DirectoryParticipant = { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas" };
// Manage sections moved from a masthead icon into the sidebar options menu.
const openManageSections = async () => {
  await userEvent.click(screen.getByRole("button", { name: "Sidebar options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Manage sections" }));
};
const base: DirectoryRoom = { roomId: "channel", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, participants: [] };

it("creates through a centered portal with member settings and closes on success", async () => {
  const onCreate = vi.fn().mockResolvedValue(true);
  const { container } = render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[alice, atlas]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={onCreate} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  const dialog = screen.getByRole("dialog", { name: "Create a channel" });
  expect(container.contains(dialog)).toBe(false);
  fireEvent.change(within(dialog).getByLabelText("Channel name"), { target: { value: "Launch" } });
  fireEvent.click(within(dialog).getByLabelText("Select Atlas"));
  expect(within(dialog).getByLabelText("Response for Atlas")).toHaveValue("active");
  fireEvent.click(within(dialog).getByRole("button", { name: "Create channel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(onCreate).toHaveBeenCalledWith({ kind: "channel", name: "Launch", members: [{ kind: "agent", id: "atlas", role: "member", listenMode: "active" }] });
});

it("preserves a failed draft and disables member controls while creating", async () => {
  const props = { ownerScopeId: "org", sections: [], state: "ready" as const, rooms: [], participants: [atlas], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onCreate: vi.fn().mockResolvedValue(false) };
  const { rerender } = render(<RoomDirectory {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  fireEvent.change(screen.getByLabelText("Channel name"), { target: { value: "Launch" } });
  fireEvent.click(screen.getByLabelText("Select Atlas"));
  rerender(<RoomDirectory {...props} creationBusy />);
  expect(screen.getByLabelText("Response for Atlas")).toBeDisabled();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  rerender(<RoomDirectory {...props} creationError="Try again" />);
  expect(screen.getByRole("alert")).toHaveTextContent("Try again");
  expect(screen.getByLabelText("Channel name")).toHaveValue("Launch");
  expect(screen.getByLabelText("Select Atlas")).toBeChecked();
  fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "New conversation" })).toHaveFocus();
});

// Room-name matchers are anchored: every row now carries a "Conversation
// actions for <name>" overflow trigger, which an unanchored /Name/ would
// match as well.
it("separates channels from direct messages, keeping group DMs — with stacked avatars — inside Direct messages, while preserving selection and unread counts", () => {
  const onSelectRoom = vi.fn();
  render(<RoomDirectory ownerScopeId="org" state="ready" sections={[]} currentRoomId="friday-dm" participants={[]}
    onSelectRoom={onSelectRoom} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} rooms={[
    base,
    { ...base, roomId: "friday-dm", kind: "dm", name: null, participants: [friday], unreadCount: 3 },
    { ...base, roomId: "group", kind: "group-dm", name: "Launch crew", participants: [alice, atlas] },
  ]} />);
  expect(within(screen.getByRole("region", { name: "Channels" })).getByRole("button", { name: /^Launch/ })).toBeInTheDocument();
  const directMessages = within(screen.getByRole("region", { name: "Direct messages" }));
  // The facepile is decorative — the row already announces "Launch crew" — so
  // the avatars are found with `hidden`.
  const groupRow = directMessages.getByRole("button", { name: /^Launch crew/ });
  expect(within(groupRow).getByRole("img", { name: "Alice", hidden: true })).toBeInTheDocument();
  expect(within(groupRow).getByRole("img", { name: "Atlas", hidden: true })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Group chats" })).not.toBeInTheDocument();
  const row = directMessages.getByRole("button", { name: /^Friday/ });
  expect(row).toHaveAttribute("aria-current", "page");
  expect(within(row).getByLabelText("3 unread messages")).toHaveTextContent("3");
  expect(within(row).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  fireEvent.click(row);
  expect(onSelectRoom).toHaveBeenCalledWith("friday-dm");
});

it("marks a conversation unread and navigates to the next unread conversation", async () => {
  const user = userEvent.setup();
  const onSelectRoom = vi.fn();
  const onMarkUnread = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" currentRoomId="channel" participants={[]}
    onSelectRoom={onSelectRoom} onSelectParticipant={vi.fn()} onMarkUnread={onMarkUnread} rooms={[
      base,
      { ...base, roomId: "first-unread", name: "First unread", unreadCount: 2 },
      { ...base, roomId: "second-unread", name: "Second unread", unreadCount: 1 },
    ]} />);

  await user.click(screen.getByRole("button", { name: "Sidebar options" }));
  await user.click(await screen.findByRole("menuitem", { name: "Jump to next unread" }));
  expect(onSelectRoom).toHaveBeenCalledWith("first-unread");
  await user.click(screen.getByRole("button", { name: "Conversation actions for Launch" }));
  await user.click(await screen.findByRole("menuitem", { name: "Mark unread" }));
  await waitFor(() => expect(onMarkUnread).toHaveBeenCalledWith("channel"));
});

it("shows mark-unread progress and lets the user retry a failure", async () => {
  const user = userEvent.setup();
  let reject!: (error: Error) => void;
  const onMarkUnread = vi.fn()
    .mockImplementationOnce(() => new Promise<void>((_, fail) => { reject = fail; }))
    .mockResolvedValueOnce(undefined);
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" participants={[]} rooms={[base]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onMarkUnread={onMarkUnread} />);

  await user.click(screen.getByRole("button", { name: "Conversation actions for Launch" }));
  await user.click(screen.getByRole("menuitem", { name: "Mark unread" }));
  // Radix returns focus to the trigger on select; it must stay focusable
  // (aria-busy, not disabled) or the browser blurs it straight to <body>.
  expect(screen.getByRole("button", { name: "Conversation actions for Launch" })).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button", { name: "Conversation actions for Launch" })).toBeEnabled();
  await act(async () => reject(new Error("offline")));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not mark Launch unread");
  await user.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(onMarkUnread).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

it("lists every person and agent under Direct messages, once each, and opens the conversation on click", () => {
  const onSelectParticipant = vi.fn();
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" onSelectRoom={vi.fn()} onSelectParticipant={onSelectParticipant}
    rooms={[{ ...base, roomId: "atlas-dm", kind: "dm", name: null, participants: [atlas] }]} participants={[friday, alice, atlas]} />);
  const directMessages = within(screen.getByRole("region", { name: "Direct messages" }));
  // Atlas already has a DM room, so he is the room row and not a second roster row.
  expect(directMessages.getAllByRole("button", { name: /^Atlas/ })).toHaveLength(1);
  // Friday (roster row) and Atlas (DM room row) are agents; Alice is not.
  expect(directMessages.getAllByText("Agent")).toHaveLength(2);
  fireEvent.click(directMessages.getByRole("button", { name: /^Friday/ }));
  fireEvent.click(directMessages.getByRole("button", { name: /^Alice/ }));
  expect(onSelectParticipant).toHaveBeenNthCalledWith(1, friday);
  expect(onSelectParticipant).toHaveBeenNthCalledWith(2, alice);
  expect(screen.queryByRole("button", { name: "People and agents" })).not.toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
});

it("marks people who are online with a presence dot, never agents", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    participants={[{ ...alice, online: true }, { ...alice, id: "bob", displayName: "Bob", online: false }, friday]} />);
  const directMessages = within(screen.getByRole("region", { name: "Direct messages" }));
  const aliceRow = directMessages.getByRole("button", { name: /^Alice/ });
  expect(within(aliceRow).getByText("Online")).toBeInTheDocument();
  expect(within(aliceRow).getByRole("img", { name: "Alice" }).parentElement).toHaveAttribute("data-presence", "online");
  expect(within(directMessages.getByRole("button", { name: /^Bob/ })).queryByText("Online")).not.toBeInTheDocument();
  expect(within(directMessages.getByRole("button", { name: /^Friday/ })).queryByText("Online")).not.toBeInTheDocument();
});

it("shows a participant-selection error under the heading", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[alice]} onSelectRoom={vi.fn()}
    onSelectParticipant={vi.fn()} creationError="Conversation could not be opened." />);
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation could not be opened.");
});

it("keeps available rooms visible with a detail recovery notice", () => {
  const onRetry = vi.fn();
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="error" rooms={[
    base,
    { ...base, roomId: "friday-dm", kind: "dm", name: null, participants: [friday] },
  ]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onRetry={onRetry} />);

  expect(screen.getByRole("button", { name: /^Launch/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Friday/ })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Some conversation details could not be loaded.");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(onRetry).toHaveBeenCalledOnce();
});

it("keeps an empty directory error blocking", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="error" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onRetry={vi.fn()} />);

  expect(screen.getByRole("status")).toHaveTextContent("Conversations could not be loaded.");
  expect(screen.queryByRole("button", { name: /Launch/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
});

const directory = { ownerScopeId: "org", sections: [], state: "ready" as const, rooms: [base], participants: [atlas], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn() };

it("lists rooms with a saved draft under Drafts and marks the row", () => {
  render(<RoomDirectory {...directory} draftRoomIds={["channel"]} />);
  const drafts = screen.getByRole("region", { name: "Drafts" });
  expect(within(drafts).getByRole("button", { name: /Launch/ })).toBeInTheDocument();
  // Only the room-list row is labelled; the Drafts row's pencil is decorative,
  // since its own section heading already says Drafts.
  expect(screen.getAllByLabelText("Unsent draft")).toHaveLength(1);
});

it("renders no Drafts section when nothing is drafted", () => {
  render(<RoomDirectory {...directory} draftRoomIds={[]} />);
  expect(screen.queryByRole("region", { name: "Drafts" })).not.toBeInTheDocument();
});

it("renders no Drafts section when the drafted room isn't in the directory", () => {
  render(<RoomDirectory {...directory} draftRoomIds={["gone"]} />);
  expect(screen.queryByRole("region", { name: "Drafts" })).not.toBeInTheDocument();
});

it("renders Starred above Channels and stars a room from its overflow menu", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()} rooms={[
      { ...base, roomId: "plain", name: "Plain" },
      { ...base, roomId: "fav", name: "Favourite", starred: true },
    ]} />);
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Starred", "Channels"]);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { starred: true });
});

it("mutes a room, drops its badge, and says so without relying on colour", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()} rooms={[
      { ...base, roomId: "loud", name: "Loud", unreadCount: 4 },
      { ...base, roomId: "quiet", name: "Quiet", unreadCount: 7, muted: true },
    ]} />);
  expect(screen.getByLabelText("4 unread messages")).toBeInTheDocument();
  expect(screen.queryByLabelText("7 unread messages")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Quiet/ })).toHaveTextContent("Muted");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Quiet" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Unmute conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith("quiet", { muted: false });
});

it("surfaces a failed star without leaving the row looking starred", async () => {
  const onSetPrefs = vi.fn().mockRejectedValue(new Error("offline"));
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()}
    rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  expect(screen.queryByRole("heading", { name: "Starred" })).not.toBeInTheDocument();
});

it("returns focus to the row's own trigger after starring re-parents it into Starred", async () => {
  let resolveSetPrefs!: () => void;
  const onSetPrefs = vi.fn(() => new Promise<void>((done) => { resolveSetPrefs = done; }));
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs, onMarkUnread: vi.fn() };
  const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { starred: true });
  // Mirrors the real setRoomPrefs (useRooms.ts): the optimistic re-bucket into
  // Starred lands on the `rooms` prop while the request is still in flight --
  // well before this action's own promise settles -- moving the row (and its
  // overflow trigger) into a different DOM subtree.
  rerender(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain", starred: true }]} />);
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Starred"]);
  await act(async () => resolveSetPrefs());
  await waitFor(() => expect(screen.getByRole("button", { name: "Conversation actions for Plain" })).toHaveFocus());
});

it("keeps a failed star's error banner across a room switch", async () => {
  const onSetPrefs = vi.fn().mockRejectedValue(new Error("offline"));
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs, onMarkUnread: vi.fn(),
    rooms: [{ ...base, roomId: "plain", name: "Plain" }] };
  const { rerender } = render(<RoomDirectory {...props} currentRoomId="plain" />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("offline");
  // Only currentRoomId changes here -- the directory must not remount (and
  // silently drop the banner) just because the viewer opened another room.
  rerender(<RoomDirectory {...props} currentRoomId="other" />);
  expect(screen.getByRole("alert")).toHaveTextContent("offline");
});

it("guards against a double star while the row's own save is still pending", async () => {
  let resolveSetPrefs!: () => void;
  const onSetPrefs = vi.fn(() => new Promise<void>((done) => { resolveSetPrefs = done; }));
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={onSetPrefs} onMarkUnread={vi.fn()}
    rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  expect(onSetPrefs).toHaveBeenCalledTimes(1);
  // Reopen the row menu while the first save is still in flight -- the
  // trigger stays enabled (so focus restoration keeps working), but the item
  // itself is disabled, and setRoomPrefs' own guard must hold even if it
  // weren't.
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  const item = await screen.findByRole("menuitem", { name: "Star conversation" });
  expect(item).toHaveAttribute("data-disabled");
  await userEvent.click(item);
  expect(onSetPrefs).toHaveBeenCalledTimes(1);
  await act(async () => resolveSetPrefs());
});

it("focuses the destination group's header when starring collapses the row out of view", async () => {
  window.localStorage.clear();
  window.localStorage.setItem("lightbulb.messaging.sidebar.org", JSON.stringify({ collapsed: ["starred"], sort: "alpha" }));
  let resolveSetPrefs!: () => void;
  const onSetPrefs = vi.fn(() => new Promise<void>((done) => { resolveSetPrefs = done; }));
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs, onMarkUnread: vi.fn() };
  const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Star conversation" }));
  // Starred is already collapsed and "plain" is not the open room, so once it
  // re-buckets into Starred its row (and overflow trigger) never renders at all.
  rerender(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain", starred: true }]} />);
  expect(screen.queryByRole("button", { name: "Conversation actions for Plain" })).not.toBeInTheDocument();
  await act(async () => resolveSetPrefs());
  await waitFor(() => expect(screen.getByRole("button", { name: "Expand Starred" })).toHaveFocus());
});

it("sorts unread first when asked and remembers the choice per owner scope", async () => {
  window.localStorage.clear();
  const rooms = [
    { ...base, roomId: "alpha", name: "Alpha" },
    { ...base, roomId: "zulu", name: "Zulu", unreadCount: 2 },
  ];
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [], rooms,
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn(), onMarkUnread: vi.fn() };
  const { unmount } = render(<RoomDirectory {...props} />);
  const names = () => screen.getAllByTitle(/^(Alpha|Zulu)$/).map((node) => node.textContent);
  expect(names()).toEqual(["Alpha", "Zulu"]);
  await userEvent.click(screen.getByRole("button", { name: "Sidebar options" }));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "Unread first" }));
  expect(names()).toEqual(["Zulu", "Alpha"]);
  expect(window.localStorage.getItem("lightbulb.messaging.sidebar.org")).toContain('"sort":"unread"');
  unmount();
  render(<RoomDirectory {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "Sidebar options" }));
  expect(await screen.findByRole("menuitemradio", { name: "Unread first" })).toHaveAttribute("aria-checked", "true");
});

it("keeps a saved session column width when a sort/collapse change rewrites sidebar prefs", async () => {
  window.localStorage.clear();
  // Its own key, separate from the sidebar-prefs blob -- a sort/collapse
  // rewrite of the blob below must not touch it.
  window.localStorage.setItem("lightbulb.messaging.sessionColumnWidth.org", "480");
  const rooms = [
    { ...base, roomId: "alpha", name: "Alpha" },
    { ...base, roomId: "zulu", name: "Zulu", unreadCount: 2 },
  ];
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]} rooms={rooms}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Sidebar options" }));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "Unread first" }));
  expect(window.localStorage.getItem("lightbulb.messaging.sessionColumnWidth.org")).toBe("480");
  expect(JSON.parse(window.localStorage.getItem("lightbulb.messaging.sidebar.org")!)).not.toHaveProperty("sessionColumnWidth");
});

it("collapses a group and keeps the collapse across a remount", async () => {
  window.localStorage.clear();
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    rooms: [{ ...base, roomId: "plain", name: "Plain" }],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn(), onMarkUnread: vi.fn() };
  const { unmount } = render(<RoomDirectory {...props} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse Channels" }));
  expect(screen.queryByRole("button", { name: /^Plain/ })).not.toBeInTheDocument();
  unmount();
  render(<RoomDirectory {...props} />);
  expect(screen.getByRole("button", { name: "Expand Channels" })).toBeInTheDocument();
});

it("keeps a collapsed group's unread count on its header", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} rooms={[
      { ...base, roomId: "loud", name: "Loud", unreadCount: 4 },
      { ...base, roomId: "hushed", name: "Hushed", unreadCount: 9, muted: true },
    ]} />);
  const toggle = screen.getByRole("button", { name: "Collapse Channels" });
  expect(toggle).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(toggle);
  expect(screen.queryByRole("button", { name: /^Loud/ })).not.toBeInTheDocument();
  // The muted room's 9 stay out of the count, exactly as they stay off its row.
  expect(screen.getByLabelText("4 unread messages in Channels")).toHaveTextContent("4");
  expect(screen.getByRole("button", { name: "Expand Channels" })).toHaveAttribute("aria-expanded", "false");
});

it("keeps the open room visible inside a collapsed group", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]} currentRoomId="open"
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} rooms={[
      { ...base, roomId: "open", name: "Open" },
      { ...base, roomId: "other", name: "Other" },
    ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse Channels" }));
  expect(screen.queryByRole("button", { name: /^Other/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Open/ })).toHaveAttribute("aria-current", "page");
});

it("leaves out menu items the caller cannot act on", async () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" participants={[]}
    rooms={[{ ...base, roomId: "plain", name: "Plain" }]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  expect(await screen.findByRole("menuitem", { name: "Copy link" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Star conversation" })).not.toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Mark unread" })).not.toBeInTheDocument();
});

it("creates a named group chat with a flexible mix of participants", async () => {
  const onCreate = vi.fn().mockResolvedValue(true);
  render(<RoomDirectory ownerScopeId="org" state="ready" sections={[]} rooms={[]} participants={[alice, atlas]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={onCreate} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  fireEvent.change(screen.getByLabelText("Conversation type"), { target: { value: "group-dm" } });
  const dialog = screen.getByRole("dialog", { name: "New group chat" });
  fireEvent.change(within(dialog).getByLabelText("Group chat name (optional)"), { target: { value: "Launch crew" } });
  fireEvent.click(within(dialog).getByLabelText("Select Alice"));
  fireEvent.click(within(dialog).getByLabelText("Select Atlas"));
  fireEvent.click(within(dialog).getByRole("button", { name: "Start group chat" }));
  await waitFor(() => expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({
    kind: "group-dm", name: "Launch crew",
    members: expect.arrayContaining([expect.objectContaining({ kind: "human", id: "alice" }), expect.objectContaining({ kind: "agent", id: "atlas" })]),
  })));
});

// Regression: the footer once reused `.room-composer-actions` (the message
// composer's 28px icon-button row), which squeezed "Cancel" and the primary
// button into overlapping tiny boxes. Both buttons must render, side by side,
// in the same right-aligned DialogFooter every other dialog here uses.
it("renders the New group chat footer as a normal, non-overlapping DialogFooter row", () => {
  render(<RoomDirectory ownerScopeId="org" state="ready" sections={[]} rooms={[]} participants={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  fireEvent.change(screen.getByLabelText("Conversation type"), { target: { value: "group-dm" } });
  const dialog = screen.getByRole("dialog", { name: "New group chat" });
  const cancel = within(dialog).getByRole("button", { name: "Cancel" });
  const create = within(dialog).getByRole("button", { name: "Start group chat" });
  expect(cancel).toBeVisible();
  expect(create).toBeVisible();
  const footer = cancel.parentElement;
  expect(footer).toBe(create.parentElement);
  expect(footer).toHaveClass("gap-2");
  expect(footer).not.toHaveClass("room-composer-actions");
});

/**
 * jsdom gives every element a 0×0 rect at the origin, and @dnd-kit's keyboard
 * sorting is pure geometry — with no heights, ArrowDown finds nothing below.
 * Stack the rendered groups 100px apart so a drag has somewhere to go. The
 * global afterEach's `vi.restoreAllMocks()` puts the real method back.
 */
function stackGroupRects() {
  const real = Element.prototype.getBoundingClientRect;
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(function (this: Element) {
    const index = [...document.querySelectorAll(".room-directory section[aria-label]")].indexOf(this);
    if (index < 0) return real.call(this);
    const top = index * 100;
    return { top, bottom: top + 100, left: 0, right: 200, width: 200, height: 100, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
  });
}

const sectionProps = {
  ownerScopeId: "org", state: "ready" as const, participants: [],
  onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSetPrefs: vi.fn().mockResolvedValue(undefined),
  onMarkUnread: vi.fn(), onSaveSections: vi.fn().mockResolvedValue(undefined),
  onLeaveRoom: vi.fn().mockResolvedValue(undefined),
};

it("creates a section, renames it, and deletes it from the manage dialog", async () => {
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections} sections={[{ id: "s-1", name: "Ship" }]}
    rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Admin");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add section" }));
  expect(onSaveSections).toHaveBeenCalledWith([
    { id: "s-1", name: "Ship" },
    { id: expect.any(String), name: "Admin" },
  ]);

  onSaveSections.mockClear();
  await userEvent.clear(within(dialog).getByLabelText("Rename Ship"));
  await userEvent.type(within(dialog).getByLabelText("Rename Ship"), "Shipping");
  await userEvent.click(within(dialog).getByRole("button", { name: "Save sections" }));
  expect(onSaveSections).toHaveBeenLastCalledWith([{ id: "s-1", name: "Shipping" }]);

  // Saving closes the dialog, so the delete runs against a freshly opened one.
  // The mock never updates `sections`, so the row is still named "Ship".
  onSaveSections.mockClear();
  await openManageSections();
  const reopened = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.click(within(reopened).getByRole("button", { name: "Delete Ship" }));
  const confirm = await within(reopened).findByRole("alert");
  expect(within(reopened).getByRole("button", { name: "Cancel" })).toHaveFocus();
  await userEvent.click(within(confirm).getByRole("button", { name: "Delete section" }));
  expect(onSaveSections).toHaveBeenLastCalledWith([]);
});

// Same regression as the New group chat footer: the delete-section confirm
// shared `.room-composer-actions` too, and got the same overlapping buttons.
it("renders the delete-section confirm footer as a normal DialogFooter row", async () => {
  render(<RoomDirectory {...sectionProps} sections={[{ id: "s-1", name: "Ship" }]}
    rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.click(within(dialog).getByRole("button", { name: "Delete Ship" }));
  const confirm = await within(dialog).findByRole("alert");
  const cancel = within(confirm).getByRole("button", { name: "Cancel" });
  const del = within(confirm).getByRole("button", { name: "Delete section" });
  expect(cancel).toBeVisible();
  expect(del).toBeVisible();
  const footer = cancel.parentElement;
  expect(footer).toBe(del.parentElement);
  expect(footer).toHaveClass("gap-2");
  expect(footer).not.toHaveClass("room-composer-actions");
});

// Regression: the row menu's "Move to <section>" item used to stay missing
// until a full reload after creating that section in the same session (a
// useRooms cache bug, since fixed) -- this exercises the RoomDirectory half
// of that contract: once `sections` includes the new entry (whatever handed
// it that -- the real hook, or here, a rerender standing in for it), the row
// menu offers it immediately, with no remount.
it("offers Move to <name> in the row menu right after that section is created, without a remount", async () => {
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const props = { ...sectionProps, onSaveSections, onSetPrefs };
  const { rerender } = render(<RoomDirectory {...props} sections={[]} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);

  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Ops");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add section" }));
  expect(onSaveSections).toHaveBeenCalledWith([{ id: expect.any(String), name: "Ops" }]);
  const savedSectionId: string = onSaveSections.mock.calls[0][0][0].id;
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));

  // Stands in for the corrected hook handing back the new section on the
  // very next render, in the same session -- not a remount/reload.
  rerender(<RoomDirectory {...props} sections={[{ id: savedSectionId, name: "Ops" }]} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);

  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Move to Ops" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { sectionId: savedSectionId });
});

it("moves a room into a section and back out from the row menu", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const props = { ...sectionProps, onSetPrefs, sections: [{ id: "s-1", name: "Ship" }] };
  const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Move to Ship" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { sectionId: "s-1" });

  // Once it is in a section the menu offers the way back out (Task 7 names
  // that item "Remove from section"), which writes a null sectionId.
  rerender(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain", sectionId: "s-1" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Remove from section" }));
  expect(onSetPrefs).toHaveBeenLastCalledWith("plain", { sectionId: null });
});

it("returns focus to the sidebar options button when the Manage sections dialog closes", async () => {
  render(<RoomDirectory {...sectionProps} sections={[{ id: "s-1", name: "Ship" }]} rooms={[]} />);
  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Manage sections" })).not.toBeInTheDocument());
  expect(screen.getByRole("button", { name: "Sidebar options" })).toHaveFocus();
});

it("refuses to save, add or delete while a section name is blank", async () => {
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections}
    sections={[{ id: "s-1", name: "Ship" }, { id: "s-2", name: "Admin" }]} rooms={[]} />);
  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.clear(within(dialog).getByLabelText("Rename Ship"));
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Admin");
  expect(within(dialog).getByLabelText("Rename Ship")).toHaveAttribute("aria-invalid", "true");
  expect(within(dialog).getByText("Every section needs a name.")).toBeInTheDocument();
  // A blank rename must not ride along with an unrelated add or delete.
  expect(within(dialog).getByRole("button", { name: "Add section" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "Delete Admin" })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: "Save sections" })).toBeDisabled();
  expect(onSaveSections).not.toHaveBeenCalled();
});

// vitest's config here has no `css: true`, so imported stylesheets never
// reach jsdom and getComputedStyle/toHaveStyle can't see them -- assert
// against the source text instead of a rendered computed style.
it("keeps the section drag grip usable on touch and above the 44px touch floor", () => {
  const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
  // PointerSensor claims the gesture only once the browser isn't allowed to
  // treat it as a scroll of `.room-directory` first.
  expect(css).toMatch(/\.room-directory-grip\s*\{[^}]*touch-action:\s*none;/);
  expect(css).toMatch(/\.room-directory-actions,\s*\.room-directory-grip\s*\{\s*min-width:\s*44px;\s*\}/);
});

// The heading's title is a direct <h2> child of .room-directory-heading (the
// CSS ellipsis selector below targets it structurally) -- jsdom won't apply
// the stylesheet, so this pairs a DOM-structure assertion with a source-text
// assertion of the rules instead of a computed-style one.
it("keeps the sidebar heading from forcing the directory wider than its box", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={vi.fn()} onSaveSections={vi.fn()} />);
  const heading = screen.getByText("Conversations");
  expect(heading.tagName).toBe("H2");
  expect(heading.parentElement).toHaveClass("room-directory-heading");
  const actions = heading.nextElementSibling;
  expect(actions).toHaveClass("room-directory-heading-actions");

  const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
  expect(css).toMatch(/\.room-directory\s*\{[^}]*overflow-x:\s*hidden;/);
  expect(css).toMatch(/\.room-directory-heading\s*\{[^}]*flex-wrap:\s*wrap;/);
  expect(css).toMatch(/\.room-directory-heading\s*>\s*h2\s*\{[^}]*min-width:\s*0;[^}]*overflow:\s*hidden;[^}]*text-overflow:\s*ellipsis;[^}]*white-space:\s*nowrap;/);
  expect(css).toMatch(/\.room-directory-heading-actions\s*\{[^}]*flex-shrink:\s*0;/);
  // When the actions cluster wraps onto its own line it's the lone flex item
  // there, so .room-directory-heading's space-between has nothing left to
  // space and collapses flush-left. margin-left: auto (not justify-content,
  // which would only distribute space among the cluster's OWN children) is
  // what pushes a flex item to the end of its own flex line.
  expect(css).toMatch(/\.room-directory-heading-actions\s*\{[^}]*margin-left:\s*auto;/);
  expect(css).toMatch(/\.room-directory-heading-actions,\s*\.room-directory-row\s*\{[^}]*min-width:\s*0;/);
});

it("reorders sections with the keyboard and persists the new order", async () => {
  stackGroupRects();
  const onSaveSections = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections}
    sections={[{ id: "s-1", name: "Ship" }, { id: "s-2", name: "Admin" }]}
    rooms={[
      { ...base, roomId: "a", name: "A", sectionId: "s-1" },
      { ...base, roomId: "b", name: "B", sectionId: "s-2" },
    ]} />);
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Ship", "Admin"]);
  screen.getByRole("button", { name: "Reorder Ship" }).focus();
  await userEvent.keyboard("{ }{ArrowDown}{ }");
  await waitFor(() => expect(onSaveSections).toHaveBeenCalledWith([{ id: "s-2", name: "Admin" }, { id: "s-1", name: "Ship" }]));
});

it("offers Leave on a channel only, and confirms before leaving", async () => {
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onLeaveRoom={onLeaveRoom} sections={[]} rooms={[
    { ...base, roomId: "chan", name: "Channel" },
    { ...base, roomId: "dm", kind: "dm", name: "Direct", participants: [alice] },
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Direct" }));
  expect(screen.queryByRole("menuitem", { name: "Leave channel" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Channel" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Leave channel" }));
  const confirm = await screen.findByRole("dialog", { name: "Leave Channel?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Leave channel" }));
  expect(onLeaveRoom).toHaveBeenCalledWith("chan");
});

it("offers Leave on a group chat too", async () => {
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onLeaveRoom={onLeaveRoom} sections={[]} rooms={[
    { ...base, roomId: "trio", kind: "group-dm", name: "Trio", participants: [alice] },
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Trio" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Leave group chat" }));
  const confirm = await screen.findByRole("dialog", { name: "Leave Trio?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Leave group chat" }));
  expect(onLeaveRoom).toHaveBeenCalledWith("trio");
});

it("surfaces a failed section save without losing the edit", async () => {
  const onSaveSections = vi.fn().mockRejectedValue(new Error("Sections could not be saved."));
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections} sections={[]} rooms={[]} />);
  await openManageSections();
  const dialog = await screen.findByRole("dialog", { name: "Manage sections" });
  await userEvent.type(within(dialog).getByLabelText("New section name"), "Admin");
  await userEvent.click(within(dialog).getByRole("button", { name: "Add section" }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("Sections could not be saved.");
  expect(within(dialog).getByLabelText("New section name")).toHaveValue("Admin");
});

it("reverts an optimistic reorder when the save fails", async () => {
  stackGroupRects();
  const onSaveSections = vi.fn().mockRejectedValue(new Error("Sections could not be saved."));
  render(<RoomDirectory {...sectionProps} onSaveSections={onSaveSections}
    sections={[{ id: "s-1", name: "Ship" }, { id: "s-2", name: "Admin" }]}
    rooms={[
      { ...base, roomId: "a", name: "A", sectionId: "s-1" },
      { ...base, roomId: "b", name: "B", sectionId: "s-2" },
    ]} />);
  screen.getByRole("button", { name: "Reorder Ship" }).focus();
  await userEvent.keyboard("{ }{ArrowDown}{ }");
  expect(await screen.findByRole("alert")).toHaveTextContent("Sections could not be saved.");
  expect(screen.getAllByRole("heading", { level: 3 }).map((node) => node.textContent)).toEqual(["Ship", "Admin"]);
});

it("separates shared channels into External connections, retains their indicator, and preserves actions", async () => {
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onSetPrefs={vi.fn()} onMarkUnread={vi.fn()} onLeaveRoom={vi.fn()} rooms={[
      { ...base, roomId: "own", name: "Launch" },
      { ...base, roomId: "ext", name: "lightbulb-acme", shared: true },
    ]} />);
  const channels = screen.getByRole("region", { name: "Channels" });
  const external = screen.getByRole("region", { name: "External connections" });
  expect(within(channels).getByRole("button", { name: /^Launch/ })).toBeInTheDocument();
  expect(within(channels).queryByRole("button", { name: /^lightbulb-acme/ })).not.toBeInTheDocument();
  const shared = within(external).getByRole("button", { name: /^lightbulb-acme/ });
  expect(screen.getAllByRole("button", { name: /^lightbulb-acme/ })).toHaveLength(1);
  expect(shared).toHaveTextContent("Shared");
  expect(within(shared).getByText("Shared")).toHaveAttribute("title", "Your direct line to the Lightbulb team");
  expect(within(shared).getByText("Your direct line to the Lightbulb team")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Launch/ })).not.toHaveTextContent("direct line");
  expect(screen.getByRole("button", { name: /^Launch/ })).not.toHaveTextContent("Shared");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for Launch" }));
  expect(await screen.findByRole("menuitem", { name: /Leave/ })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Conversation actions for lightbulb-acme" }));
  expect(screen.queryByRole("menuitem", { name: /Leave/ })).not.toBeInTheDocument();
});

it("hides empty Channels and External connections sections", () => {
  const props = { ownerScopeId: "org", state: "ready" as const, participants: [], sections: [],
    onSelectRoom: vi.fn(), onSelectParticipant: vi.fn() };
  const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, shared: true }]} />);
  expect(screen.getByRole("region", { name: "External connections" })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "Channels" })).not.toBeInTheDocument();
  rerender(<RoomDirectory {...props} rooms={[base]} />);
  expect(screen.getByRole("region", { name: "Channels" })).toBeInTheDocument();
  expect(screen.queryByRole("region", { name: "External connections" })).not.toBeInTheDocument();
});

it("keeps the selected external channel visible when External connections is collapsed", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]} currentRoomId="selected-external"
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} rooms={[
      { ...base, roomId: "selected-external", name: "Selected external", shared: true, unreadCount: 2 },
      { ...base, roomId: "other-external", name: "Other external", shared: true, unreadCount: 3 },
    ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse External connections" }));
  expect(screen.queryByRole("button", { name: /^Other external/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: /^Selected external/ })).toHaveAttribute("aria-current", "page");
  expect(screen.getByLabelText("5 unread messages in External connections")).toHaveTextContent("5");
  expect(screen.getByRole("button", { name: "Expand External connections" })).toHaveAttribute("aria-expanded", "false");
});

it("lists live sessions above the room groups, hides the section without a session door, and opens one on click", async () => {
  const onSelectSession = vi.fn();
  const session = { sessionId: "s1", deviceId: "d", humanUserId: "alice", provider: "claude" as const, providerVersion: "2", generation: "g", status: "busy" as const, activeTurnId: null,
    capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, title: "Fix login", lastEventAt: new Date().toISOString() };
  const { rerender } = render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    sessions={[{ session, ownerName: "Alice", own: false }]} onSelectSession={onSelectSession} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(section.compareDocumentPosition(screen.getByRole("region", { name: "Channels" })) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const row = within(section).getByRole("button", { name: /Open Claude · Fix login, Alice, Working/ });
  expect(row).not.toHaveTextContent("Claude ·");
  expect(within(row).getByText("Fix login")).toBeInTheDocument();
  expect(row.querySelector(".session-tool-badge .native-session-dot-busy")).not.toBeNull();
  await userEvent.click(row);
  expect(onSelectSession).toHaveBeenCalledWith("s1");
  rerender(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} sessions={[]} />);
  expect(screen.queryByRole("region", { name: "Sessions" })).not.toBeInTheDocument();
});

it("keeps Sessions visible when nobody shares, with a Start a session guide", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org-empty" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    sessions={[]} onSelectSession={vi.fn()} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(within(section).getByText("No one is sharing a session.")).toBeInTheDocument();
  await userEvent.click(within(section).getByRole("button", { name: "Start a session" }));
  const dialog = await screen.findByRole("dialog", { name: "Share a session" });
  expect(within(dialog).getByRole("button", { name: "Copy lightbulb claude" })).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: "Copy lightbulb codex" })).toBeInTheDocument();
  expect(within(dialog).getByRole("link", { name: /Session sharing/ })).toHaveAttribute("href", "/settings?panel=desktop");
});

const makeSession = (sessionId: string, title: string, status: "busy" | "idle" | "offline" | "ended") => ({ sessionId, deviceId: "d", humanUserId: "alice", provider: "claude" as const, providerVersion: "2", generation: "g",
  status, activeTurnId: null, capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, title });

it("lists only live sessions, marks the open and unread ones, and links to All sessions with the total", async () => {
  const onSelectSession = vi.fn();
  const onOpenAllSessions = vi.fn();
  render(<RoomDirectory ownerScopeId="org-live" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    onSelectSession={onSelectSession} onOpenAllSessions={onOpenAllSessions} currentSessionId="a" sessions={[
      { session: makeSession("a", "Fix login", "busy"), ownerName: "Alice", own: false, unread: null },
      { session: makeSession("b", "Ship docs", "idle"), ownerName: "Alice", own: false, unread: "finished" },
      { session: makeSession("c", "Gone quiet", "offline"), ownerName: "Alice", own: false },
      { session: makeSession("d", "Old refactor", "ended"), ownerName: "Alice", own: false },
    ]} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(within(section).getAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label"))).toEqual([
    expect.stringMatching(/Fix login/), expect.stringMatching(/Ship docs, Alice, Idle, finished since you looked/)]);
  expect(within(section).getByRole("button", { name: /Fix login/ })).toHaveAttribute("aria-current", "page");
  expect(within(section).queryByRole("searchbox")).not.toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /Recent|Archived/ })).not.toBeInTheDocument();
  await userEvent.click(within(section).getByRole("button", { name: /Ship docs/ }));
  expect(onSelectSession).toHaveBeenCalledWith("b");
  const all = within(section).getByRole("button", { name: /All sessions/ });
  expect(all).toHaveTextContent("4");
  await userEvent.click(all);
  expect(onOpenAllSessions).toHaveBeenCalled();
});

it("caps the live list at five with +N more that expands in place", async () => {
  const live = Array.from({ length: 7 }, (_, i) => ({ session: makeSession(`s${i}`, `Task ${i}`, "busy"), ownerName: "Alice", own: false }));
  render(<RoomDirectory ownerScopeId="org-cap" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    onSelectSession={vi.fn()} currentSessionId="s6" sessions={live} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  const titles = () => within(section).getAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label")!.match(/Task \d/)![0]);
  // The open session stays on screen past the cap.
  expect(titles()).toEqual(["Task 0", "Task 1", "Task 2", "Task 3", "Task 4", "Task 6"]);
  await userEvent.click(within(section).getByRole("button", { name: "+2 more" }));
  expect(titles()).toHaveLength(7);
  await userEvent.click(within(section).getByRole("button", { name: "Show fewer" }));
  expect(titles()).toHaveLength(6);
});

it("collapses the Sessions section through the shared prefs and keeps a count", async () => {
  const session = { sessionId: "s1", deviceId: "d", humanUserId: "me", provider: "codex" as const, providerVersion: "0.149.0", generation: "g", status: "idle" as const, activeTurnId: null,
    capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0 };
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} sessions={[{ session, ownerName: "Me", own: true }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse Sessions" }));
  expect(screen.getByLabelText("1 live session")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Open / })).not.toBeInTheDocument();
});

it("starts the Archived group collapsed and expands it on request", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org-archived" state="ready" sections={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    rooms={[base, { ...base, roomId: "old", name: "Old launch", archivedAt: "2026-09-23T00:00:00Z" }]} />);
  expect(screen.queryByRole("button", { name: /^Old launch/ })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Expand Archived" }));
  expect(screen.getByRole("button", { name: /^Old launch/ })).toBeInTheDocument();
});

it("nudges a lone founder to invite people, and copies the join link from the nudge", async () => {
  const onInvitePeople = vi.fn(async () => undefined);
  const { rerender } = render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[atlas]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onInvitePeople={onInvitePeople} />);
  expect(screen.getByText(/just you and your agents here/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Invite people" }));
  expect(onInvitePeople).toHaveBeenCalledTimes(1);
  expect(await screen.findByText("Copied. Anyone who opens it and signs in joins this workspace.")).toHaveAttribute("role", "status");
  rerender(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[atlas, alice]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} />);
  expect(screen.queryByText(/just you and your agents here/)).not.toBeInTheDocument();
});

it("offers Add people in a row's menu only where the viewer may add, and asks for that room's picker", async () => {
  const me = { kind: "human" as const, id: "me", displayName: "Me", mentionHandle: null, listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
  const onAddPeople = vi.fn();
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" participants={[]} currentUserId="me"
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onAddPeople={onAddPeople} rooms={[
      { ...base, roomId: "mine", name: "Mine", members: [{ ...me, role: "owner" }] },
      { ...base, roomId: "theirs", name: "Theirs", members: [{ ...me, role: "member" }] },
      { ...base, roomId: "vendor", name: "Vendor", shared: true, members: [{ ...me, role: "owner" }] },
      { ...base, roomId: "crew", kind: "group-dm", name: "Crew", participants: [alice, atlas], members: [{ ...me, role: "member" }] },
      { ...base, roomId: "pair", kind: "dm", name: null, participants: [alice], members: [{ ...me, role: "member" }] },
    ]} />);
  const menuFor = async (label: string) => {
    await userEvent.click(screen.getByRole("button", { name: `Conversation actions for ${label}` }));
    const item = screen.queryByRole("menuitem", { name: "Add people" });
    if (!item) await userEvent.keyboard("{Escape}");
    return item;
  };
  expect(await menuFor("Theirs")).toBeNull();
  expect(await menuFor("Vendor")).toBeNull();
  expect(await menuFor("Crew")).not.toBeNull();
  await userEvent.keyboard("{Escape}");
  // A direct message takes anyone too: adding a third person turns it into a group chat in place.
  expect(await menuFor("Alice")).not.toBeNull();
  await userEvent.keyboard("{Escape}");
  await userEvent.click((await menuFor("Mine"))!);
  expect(onAddPeople).toHaveBeenCalledWith("mine");
});
