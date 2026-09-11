import { act, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { RoomDirectory } from "../RoomDirectory";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const friday: DirectoryParticipant = { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: null, agentActivity: "running" };
const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const atlas: DirectoryParticipant = { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas" };
const base: DirectoryRoom = { roomId: "channel", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, participants: [] };

it("creates through a centered portal with member settings and closes on success", async () => {
  const onCreate = vi.fn().mockResolvedValue(true);
  const { container } = render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[alice, atlas]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={onCreate} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  const dialog = screen.getByRole("dialog", { name: "Create a channel" });
  expect(container.contains(dialog)).toBe(false);
  fireEvent.change(within(dialog).getByLabelText("Channel name"), { target: { value: "Launch" } });
  fireEvent.click(within(dialog).getByLabelText("Select Atlas"));
  fireEvent.change(within(dialog).getByLabelText("Response for Atlas"), { target: { value: "active" } });
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
  const groupRow = directMessages.getByRole("button", { name: /^Alice/ });
  expect(groupRow).toHaveTextContent("Launch crew");
  expect(within(groupRow).getByRole("img", { name: "Alice" })).toBeInTheDocument();
  expect(within(groupRow).getByRole("img", { name: "Atlas" })).toBeInTheDocument();
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

  fireEvent.click(screen.getByRole("button", { name: "Next unread conversation" }));
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
  expect(screen.getByRole("button", { name: "Conversation actions for Launch" })).toBeDisabled();
  await act(async () => reject(new Error("offline")));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not mark Launch unread");
  await user.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(onMarkUnread).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

it("identifies agents without handles in People and agents", () => {
  const onSelectParticipant = vi.fn();
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[friday, alice]} onSelectRoom={vi.fn()} onSelectParticipant={onSelectParticipant} />);
  fireEvent.click(screen.getByRole("button", { name: "People and agents" }));
  expect(screen.getByText("Agent")).toBeInTheDocument();
  expect(screen.getAllByText("Person")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /Friday.*Agent/ }));
  fireEvent.click(screen.getByRole("button", { name: /Alice.*Person/ }));
  expect(onSelectParticipant).toHaveBeenNthCalledWith(1, friday);
  expect(onSelectParticipant).toHaveBeenNthCalledWith(2, alice);
});

it("shows a participant-selection error beside the open people directory", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[alice]} onSelectRoom={vi.fn()}
    onSelectParticipant={vi.fn()} creationError="Conversation could not be opened." />);
  fireEvent.click(screen.getByRole("button", { name: "People and agents" }));
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
  await userEvent.selectOptions(screen.getByLabelText("Sort conversations"), "unread");
  expect(names()).toEqual(["Zulu", "Alpha"]);
  expect(window.localStorage.getItem("lightbulb.messaging.sidebar.org")).toContain('"sort":"unread"');
  unmount();
  render(<RoomDirectory {...props} />);
  expect(screen.getByLabelText("Sort conversations")).toHaveValue("unread");
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
