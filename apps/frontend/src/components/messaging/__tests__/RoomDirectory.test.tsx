import { readFileSync } from "node:fs";
import path from "node:path";
import { act, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { mergeSessionRows, type LocalSession } from "@lightbulb/shared";
import { RoomDirectory, type RoomDirectoryProps } from "../RoomDirectory";
import type { SidebarSession } from "../roomSections";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const openInviteDialog = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/components/workspace/workspaceController")>(),
  openInviteDialog,
}));

/** SidebarSession fixtures as the rows the sidebar now takes; `viewerId` names whose sessions are the viewer's (none by default). */
const toRows = (entries: readonly SidebarSession[], viewerId?: string) => mergeSessionRows(entries.map((entry) => entry.session), [],
  { userId: viewerId, nameOf: (id) => entries.find((entry) => entry.session.humanUserId === id)?.ownerName ?? id })
  .map((row) => { const entry = entries.find((candidate) => (candidate.session.networkSessionId ?? candidate.session.sessionId) === row.id)!; return { row, owner: entry.owner, unread: entry.unread }; });

const friday: DirectoryParticipant = { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: null, agentActivity: "running" };
const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const atlas: DirectoryParticipant = { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas" };
// Manage sections moved from a masthead icon into the sidebar options menu.
const openManageSections = async () => {
  await userEvent.click(screen.getByRole("button", { name: "Sidebar options" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Manage sections" }));
};
const base: DirectoryRoom = { roomId: "channel", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, participants: [] };

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
  await user.click(screen.getByRole("button", { name: "Actions for Launch" }));
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

  await user.click(screen.getByRole("button", { name: "Actions for Launch" }));
  await user.click(screen.getByRole("menuitem", { name: "Mark unread" }));
  // Radix returns focus to the trigger on select; it must stay focusable
  // (aria-busy, not disabled) or the browser blurs it straight to <body>.
  expect(screen.getByRole("button", { name: "Actions for Launch" })).toHaveAttribute("aria-busy", "true");
  expect(screen.getByRole("button", { name: "Actions for Launch" })).toBeEnabled();
  await act(async () => reject(new Error("offline")));
  expect(screen.getByRole("alert")).toHaveTextContent("Could not mark Launch unread");
  await user.click(screen.getByRole("button", { name: "Try again" }));
  await waitFor(() => expect(onMarkUnread).toHaveBeenCalledTimes(2));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
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

  expect(screen.getByRole("status")).toHaveTextContent("Chats could not be loaded.");
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
  await userEvent.click(screen.getByRole("button", { name: "Actions for Quiet" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Unmute" }));
  expect(onSetPrefs).toHaveBeenCalledWith("quiet", { muted: false });
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
  await userEvent.click(screen.getByRole("button", { name: "Actions for Plain" }));
  expect(await screen.findByRole("menuitem", { name: "Copy link" })).toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Star" })).not.toBeInTheDocument();
  expect(screen.queryByRole("menuitem", { name: "Mark unread" })).not.toBeInTheDocument();
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

  await userEvent.click(screen.getByRole("button", { name: "Actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Move to Ops" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { sectionId: savedSectionId });
});

it("moves a room into a section and back out from the row menu", async () => {
  const onSetPrefs = vi.fn().mockResolvedValue(undefined);
  const props = { ...sectionProps, onSetPrefs, sections: [{ id: "s-1", name: "Ship" }] };
  const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Actions for Plain" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Move to Ship" }));
  expect(onSetPrefs).toHaveBeenCalledWith("plain", { sectionId: "s-1" });

  // Once it is in a section the menu offers the way back out (Task 7 names
  // that item "Remove from section"), which writes a null sectionId.
  rerender(<RoomDirectory {...props} rooms={[{ ...base, roomId: "plain", name: "Plain", sectionId: "s-1" }]} />);
  await userEvent.click(screen.getByRole("button", { name: "Actions for Plain" }));
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

it("does not reserve a second action slot on session rows", () => {
  const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
  expect(css).toMatch(/\.room-directory-row--roster:has\(\.room-directory-actions\)::after\s*\{\s*display:\s*none;/);
});

it("caps sidebar session metadata so the title keeps the remaining width", () => {
  const css = readFileSync(path.resolve(__dirname, "../messaging.css"), "utf8");
  expect(css).toMatch(/\.session-row-time[^{]*\{[^}]*max-width:\s*5rem;[^}]*text-overflow:\s*ellipsis;/);
});

// The heading's title is a direct <h2> child of .room-directory-heading (the
// CSS ellipsis selector below targets it structurally) -- jsdom won't apply
// the stylesheet, so this pairs a DOM-structure assertion with a source-text
// assertion of the rules instead of a computed-style one.
it("keeps the sidebar heading from forcing the directory wider than its box", () => {
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={vi.fn()} onSaveSections={vi.fn()} />);
  const heading = screen.getByText("Chats");
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
  await userEvent.click(screen.getByRole("button", { name: "Actions for Direct" }));
  expect(screen.queryByRole("menuitem", { name: "Leave channel" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Actions for Channel" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Leave channel" }));
  const confirm = await screen.findByRole("dialog", { name: "Leave Channel?" });
  await userEvent.click(within(confirm).getByRole("button", { name: "Leave channel" }));
  expect(onLeaveRoom).toHaveBeenCalledWith("chan");
});

it("offers no Leave to a channel's owner who is alone in it, but does once someone else is there", async () => {
  const me = { kind: "human" as const, id: "me", displayName: "Me", mentionHandle: null, role: "owner" as const, listenMode: null, joinedAt: "2026-09-10T00:00:00Z", joinedSeq: 0 };
  const other = { ...me, id: "other", role: "member" as const };
  render(<RoomDirectory {...sectionProps} currentUserId="me" sections={[]} rooms={[
    { ...base, roomId: "solo", name: "Solo", members: [me] },
    { ...base, roomId: "pair", name: "Pair", members: [me, other] },
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Actions for Solo" }));
  expect(screen.queryByRole("menuitem", { name: "Leave channel" })).not.toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Actions for Pair" }));
  expect(await screen.findByRole("menuitem", { name: "Leave channel" })).toBeInTheDocument();
});

it("offers Leave on a group chat too", async () => {
  const onLeaveRoom = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory {...sectionProps} onLeaveRoom={onLeaveRoom} sections={[]} rooms={[
    { ...base, roomId: "trio", kind: "group-dm", name: "Trio", participants: [alice] },
  ]} />);
  await userEvent.click(screen.getByRole("button", { name: "Actions for Trio" }));
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
  await userEvent.click(screen.getByRole("button", { name: "Actions for Launch" }));
  expect(await screen.findByRole("menuitem", { name: /Leave/ })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  await userEvent.click(screen.getByRole("button", { name: "Actions for lightbulb-acme" }));
  expect(screen.queryByRole("menuitem", { name: /Leave/ })).not.toBeInTheDocument();
});

it("More shows a chevron and how much is inside while closed, so it never reads as empty", () => {
  const { container } = render(<RoomDirectory ownerScopeId="org" state="ready" participants={[]} sections={[]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} rooms={[
      { ...base, roomId: "own", name: "Launch" },
      { ...base, roomId: "ext", name: "lightbulb-acme", shared: true, unreadCount: 0 },
    ]} />);
  const summary = container.querySelector(".room-directory-more > summary")!;
  expect(summary.querySelector("svg.room-directory-more-chevron")).not.toBeNull();
  expect(summary).toHaveAttribute("aria-label", "More, 1 inside");
  expect(summary).toHaveTextContent("More1");
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

it("lists live sessions below Channels, hides the section without a session door, and opens one on click", async () => {
  const onSelectSession = vi.fn();
  const session = { sessionId: "s1", deviceId: "d", humanUserId: "alice", provider: "claude" as const, providerVersion: "2", generation: "g", status: "busy" as const, activeTurnId: null,
    capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 3, title: "Fix login", cwdName: "web", lastEventAt: new Date().toISOString() };
  const { rerender } = render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    sessionRows={toRows([{ session, ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } }])} onSelectSession={onSelectSession} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(section.compareDocumentPosition(screen.getByRole("region", { name: "Channels" })) & Node.DOCUMENT_POSITION_PRECEDING).toBeTruthy();
  // One line: badge, name, dot. Owner and folder stay in the accessible label only.
  const row = within(section).getByRole("button", { name: /Open Claude · Fix login, Alice, in web, Working/ });
  expect(row).not.toHaveTextContent("Claude ·");
  expect(row).not.toHaveTextContent("Alice");
  expect(row).not.toHaveTextContent("web");
  expect(within(row).getByText("Fix login")).toBeInTheDocument();
  expect(row.querySelector(".session-working")).not.toBeNull();
  await userEvent.click(row);
  expect(onSelectSession).toHaveBeenCalledWith("s1");
  rerender(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} sessionRows={[]} />);
  expect(screen.queryByRole("region", { name: "Sessions" })).not.toBeInTheDocument();
});

it("keeps Sessions visible when nobody shares, with a Start a session guide", async () => {
  window.localStorage.clear();
  render(<RoomDirectory ownerScopeId="org-empty" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    sessionRows={[]} onSelectSession={vi.fn()} />);
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

it("lists live sessions, then ended ones until archived, marks the open and unread ones, and links to All sessions with the total", async () => {
  const onSelectSession = vi.fn();
  const onOpenAllSessions = vi.fn();
  render(<RoomDirectory ownerScopeId="org-live" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    onSelectSession={onSelectSession} onOpenAllSessions={onOpenAllSessions} currentSessionId="a" sessionRows={toRows([
      { session: makeSession("a", "Fix login", "busy"), ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 }, unread: null },
      { session: makeSession("b", "Ship docs", "idle"), ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 }, unread: "finished" },
      { session: makeSession("c", "Gone quiet", "offline"), ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } },
      { session: makeSession("d", "Old refactor", "ended"), ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } },
      // Offline for days: All sessions does not list it, so the count does not either.
      { session: { ...makeSession("e", "Lost Mac", "offline"), lastEventAt: "2026-01-01T00:00:00Z" }, ownerName: "Alice", own: false, owner: { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } },
    ])} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(within(section).getAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label"))).toEqual([
    expect.stringMatching(/Fix login/), expect.stringMatching(/Ship docs, Alice, Idle, finished since you looked/),
    expect.stringMatching(/Gone quiet/), expect.stringMatching(/Old refactor/)]);
  expect(within(section).getByRole("button", { name: /^Open .*Fix login/ })).toHaveAttribute("aria-current", "page");
  expect(within(section).queryByRole("searchbox")).not.toBeInTheDocument();
  expect(within(section).queryByRole("button", { name: /^Archived/ })).not.toBeInTheDocument();
  await userEvent.click(within(section).getByRole("button", { name: /^Open .*Ship docs/ }));
  expect(onSelectSession).toHaveBeenCalledWith("b");
  const all = within(section).getByRole("button", { name: /All sessions/ });
  expect(all).toHaveTextContent("4");
  await userEvent.click(all);
  expect(onOpenAllSessions).toHaveBeenCalled();
});

const aliceFace = { kind: "human" as const, id: "alice", displayName: "Alice", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 };

it("lists a session its person ended on purpose like any ended one, until removed (desktop #4453)", () => {
  render(<RoomDirectory ownerScopeId="org-purpose" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    onSelectSession={vi.fn()} sessionRows={toRows([
      { session: makeSession("a", "Stopped by a crash", "ended"), ownerName: "Alice", own: false, owner: aliceFace },
      { session: { ...makeSession("b", "Ended with End", "ended"), endedOnPurpose: true }, ownerName: "Alice", own: false, owner: aliceFace },
    ])} />);
  const section = screen.getByRole("region", { name: "Sessions" });
  expect(within(section).getByRole("button", { name: /^Open .*Stopped by a crash/ })).toBeInTheDocument();
  expect(within(section).getByRole("button", { name: /^Open .*Ended with End/ })).toBeInTheDocument();
});

const faceOf = (id: string, name: string) => ({ ...aliceFace, id, displayName: name });
const entryOf = (session: LocalSession, name = "Alice") => ({ session, ownerName: name, own: false, owner: faceOf(session.humanUserId, name) });
const mate = (id: string, title: string, user: string, name: string, status: Parameters<typeof makeSession>[2] | "waiting-approval" = "busy") =>
  entryOf({ ...makeSession(id, title, "busy"), humanUserId: user, status } as LocalSession, name);
const tasks = (count: number, user = "alice", name = "Alice") => Array.from({ length: count }, (_, i) => mate(`s${i}`, `Task ${i}`, user, name));
const sessionProps = (scope: string, entries: readonly SidebarSession[], viewerId?: string, extra: Partial<RoomDirectoryProps> = {}): RoomDirectoryProps => ({
  ownerScopeId: scope, sections: [], state: "ready", rooms: [base], participants: [], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSelectSession: vi.fn(),
  currentUserId: viewerId, sessionRows: toRows(entries, viewerId), ...extra });
const sessionSection = () => screen.getByRole("region", { name: "Sessions" });
const openTitles = () => within(sessionSection()).queryAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label")!.match(/Task \d+|[A-Z][a-z]+ [a-z]+/)![0]);

it("folds a group of seven to five with … 2 more, keeps the open and pinned rows past it, and remembers expanding", async () => {
  window.localStorage.clear();
  const { unmount } = render(<RoomDirectory {...sessionProps("org-fold", tasks(7))} />);
  expect(openTitles()).toHaveLength(5);
  expect(within(sessionSection()).queryByRole("button", { name: /^Alice/ })).not.toBeInTheDocument();
  await userEvent.click(within(sessionSection()).getByRole("button", { name: "… 2 more" }));
  expect(openTitles()).toHaveLength(7);
  unmount();
  const again = render(<RoomDirectory {...sessionProps("org-fold", tasks(7))} />);
  expect(openTitles()).toHaveLength(7);
  await userEvent.click(within(sessionSection()).getByRole("button", { name: "Show fewer" }));
  expect(openTitles()).toHaveLength(5);
  again.unmount();
  // The open session stays past the fold.
  const open = render(<RoomDirectory {...sessionProps("org-fold", tasks(7), undefined, { currentSessionId: "s6" })} />);
  expect(openTitles()).toEqual(["Task 0", "Task 1", "Task 2", "Task 3", "Task 4", "Task 6"]);
  expect(within(sessionSection()).getByRole("button", { name: "… 1 more" })).toBeInTheDocument();
  open.unmount();
  window.localStorage.setItem("lightbulb.messaging.sessionMarks.:org-fold", JSON.stringify({ pinned: ["s0", "s1", "s2", "s3", "s4", "s5"], archived: {} }));
  render(<RoomDirectory {...sessionProps("org-fold", tasks(7))} />);
  expect(openTitles()).toHaveLength(6);
});

it("groups by state when asked and remembers it after a remount", async () => {
  window.localStorage.clear();
  const entries = [mate("a", "Task A", "alice", "Alice", "busy"), mate("b", "Task B", "alice", "Alice", "idle")];
  const { unmount } = render(<RoomDirectory {...sessionProps("org-state", entries)} />);
  expect(screen.queryByRole("button", { name: /^Working/ })).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Session view" }));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "State" }));
  expect(within(sessionSection()).getByRole("button", { name: /^Working/ })).toBeInTheDocument();
  expect(within(sessionSection()).getByRole("button", { name: /^Idle/ })).toBeInTheDocument();
  unmount();
  render(<RoomDirectory {...sessionProps("org-state", entries)} />);
  expect(within(sessionSection()).getByRole("button", { name: /^Working/ })).toBeInTheDocument();
});

it("collapses a person group to its count and still shows its waiting row; Needs you leads with the viewer's own waiting row", async () => {
  window.localStorage.clear();
  const entries = [mate("m1", "Mine waiting", "me", "Me", "waiting-approval"), mate("m2", "Mine busy", "me", "Me"),
    mate("b1", "Bob waiting", "bob", "Bob", "waiting-approval"), mate("b2", "Bob busy", "bob", "Bob")];
  render(<RoomDirectory {...sessionProps("org-needs", entries, "me")} />);
  const groups = Array.from(sessionSection().querySelectorAll(".session-group")).map((group) => group.querySelector(".session-group-header")!.textContent);
  expect(groups).toEqual(["Needs you1", "You1", "Bob2"]);
  expect(within(within(sessionSection()).getByRole("list", { name: "Needs you" })).getByText("Mine waiting")).toBeInTheDocument();
  expect(within(within(sessionSection()).getByRole("list", { name: "Bob" })).getByText("Bob waiting")).toBeInTheDocument();
  await userEvent.click(within(sessionSection()).getByRole("button", { name: /^Bob/ }));
  expect(within(sessionSection()).getByRole("button", { name: /^Bob/ })).toHaveAttribute("aria-expanded", "false");
  expect(within(sessionSection()).getByRole("button", { name: /^Bob/ })).toHaveTextContent("2");
  expect(openTitles()).toEqual(["Mine waiting", "Mine busy", "Bob waiting"]);
  expect(within(sessionSection()).queryByRole("button", { name: /… \d+ more/ })).not.toBeInTheDocument();
});

it("puts On the network below the people, with a Parked row that is not a button", () => {
  window.localStorage.clear();
  const rows = [...toRows([mate("a", "Task A", "alice", "Alice")]), { owner: faceOf("friday", "Friday"),
    row: { id: "n1", source: "network" as const, person: { name: "Friday", userId: "friday", isViewer: false }, agent: "claude", title: "Friday inbox", state: "parked" as const } }];
  render(<RoomDirectory {...sessionProps("org-network", [])} sessionRows={rows} />);
  const headers = Array.from(sessionSection().querySelectorAll(".session-group-header")).map((header) => header.textContent);
  expect(headers).toEqual(["Alice1", "On the network1"]);
  expect(within(sessionSection()).getByText("Parked")).toBeInTheDocument();
  expect(within(sessionSection()).queryByRole("button", { name: /^Open .*Friday inbox/ })).not.toBeInTheDocument();
});

it("says what Mine hides and restores it with Show all", async () => {
  window.localStorage.clear();
  const entries = [mate("m1", "Task mine", "me", "Me"), mate("b1", "Task bob 1", "bob", "Bob"), mate("b2", "Task bob 2", "bob", "Bob")];
  render(<RoomDirectory {...sessionProps("org-mine", entries, "me")} />);
  await userEvent.click(screen.getByRole("button", { name: "Session view" }));
  await userEvent.click(await screen.findByRole("menuitemradio", { name: "Mine" }));
  expect(openTitles()).toHaveLength(1);
  await userEvent.click(within(sessionSection()).getByRole("button", { name: "2 others hidden · Show all" }));
  expect(openTitles()).toHaveLength(3);
});

it("filters sessions, opens collapsed groups for a query, clears on Escape, and says when nothing matches", async () => {
  window.localStorage.clear();
  window.localStorage.setItem("lightbulb.messaging.sidebar.org-filter", JSON.stringify({ collapsed: ["sessions:person:bob"] }));
  const entries = [...tasks(4), mate("b1", "Bob deploy", "bob", "Bob"), mate("b2", "Bob docs", "bob", "Bob")];
  render(<RoomDirectory {...sessionProps("org-filter", entries)} />);
  expect(within(sessionSection()).getByRole("button", { name: /^Bob/ })).toHaveAttribute("aria-expanded", "false");
  const filter = within(sessionSection()).getByRole("searchbox", { name: "Filter sessions" });
  await userEvent.type(filter, "deploy");
  expect(openTitles()).toEqual(["Bob deploy"]);
  await userEvent.keyboard("{Escape}");
  expect(filter).toHaveValue("");
  expect(within(sessionSection()).getByRole("button", { name: /^Bob/ })).toHaveAttribute("aria-expanded", "false");
  await userEvent.type(filter, "zzz");
  expect(within(sessionSection()).getByRole("status")).toHaveTextContent('No sessions match "zzz".');
  await userEvent.click(within(sessionSection()).getByRole("button", { name: "Clear" }));
  expect(filter).toHaveValue("");
});

it("shows no sub-header for one person, and no filter field while the list is short", () => {
  window.localStorage.clear();
  render(<RoomDirectory {...sessionProps("org-one", tasks(2))} />);
  expect(sessionSection().querySelector(".session-group-header")).toBeNull();
  expect(within(sessionSection()).queryByRole("searchbox", { name: "Filter sessions" })).not.toBeInTheDocument();
});

it("shows a brass pill with the waiting count when Sessions is collapsed", async () => {
  window.localStorage.clear();
  render(<RoomDirectory {...sessionProps("org-pill", [mate("m1", "Mine waiting", "me", "Me", "waiting-approval"), mate("m2", "Mine busy", "me", "Me")], "me")} />);
  await userEvent.click(screen.getByRole("button", { name: "Collapse Sessions" }));
  expect(screen.getByLabelText("1 waiting on you")).toHaveClass("session-count--waiting");
  expect(screen.getByLabelText("2 live sessions")).toBeInTheDocument();
});

it("renders 80 sessions in one sitting with every group present", () => {
  window.localStorage.clear();
  const people = ["a", "b", "c", "d"];
  const entries = Array.from({ length: 80 }, (_, i) => mate(`s${i}`, `Task ${i}`, people[i % 4], people[i % 4].toUpperCase()));
  render(<RoomDirectory {...sessionProps("org-80", entries)} />);
  expect(Array.from(sessionSection().querySelectorAll(".session-group-header")).map((header) => header.textContent)).toEqual(["A20", "B20", "C20", "D20"]);
  expect(openTitles()).toHaveLength(20);
});

it("pins, archives, unarchives and marks a session unread from its row menu, kept on this browser", async () => {
  const onMarkSessionUnread = vi.fn();
  const sessions = [
    { session: makeSession("a", "Live work", "busy"), ownerName: "Alice", own: false, owner: aliceFace },
    { session: makeSession("b", "Old work", "ended"), ownerName: "Alice", own: false, owner: aliceFace, unread: "finished" as const },
  ];
  const props: RoomDirectoryProps = { ownerScopeId: "org-marks", sections: [], state: "ready" as const, rooms: [base], participants: [], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSelectSession: vi.fn(), onMarkSessionUnread, sessionRows: toRows(sessions) };
  const { unmount } = render(<RoomDirectory {...props} />);
  const section = () => screen.getByRole("region", { name: "Sessions" });
  const titles = () => within(section()).getAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label")!.match(/(Live|Old) work/)![0]);
  const pick = async (title: string, item: string) => {
    await userEvent.click(within(section()).getByRole("button", { name: `Session actions for ${title}` }));
    await userEvent.click(await screen.findByRole("menuitem", { name: item }));
  };
  expect(titles()).toEqual(["Live work", "Old work"]);

  await pick("Old work", "Pin");
  expect(titles()).toEqual(["Old work", "Live work"]);
  expect(within(section()).getByRole("button", { name: /Old work.*pinned/ })).toBeInTheDocument();

  await pick("Old work", "Mark read");
  expect(onMarkSessionUnread).toHaveBeenCalledWith("b", false);
  await pick("Live work", "Mark unread");
  expect(onMarkSessionUnread).toHaveBeenCalledWith("a", true);

  await pick("Old work", "Archive");
  expect(titles()).toEqual(["Live work"]);
  // Kept across a reload, on this browser only.
  unmount();
  render(<RoomDirectory {...props} />);
  expect(titles()).toEqual(["Live work"]);
  await userEvent.click(within(section()).getByRole("button", { name: /^Archived/ }));
  await pick("Old work", "Unarchive");
  // Archiving unpinned it: back among the ended rows, not on top.
  expect(titles()).toEqual(["Live work", "Old work"]);
});

it("keeps pins and archives per person: another member on the same browser does not see them", async () => {
  const sessions = [
    { session: makeSession("a", "Live work", "busy"), ownerName: "Alice", own: false, owner: aliceFace },
    { session: makeSession("b", "Old work", "ended"), ownerName: "Alice", own: false, owner: aliceFace },
  ];
  const props: RoomDirectoryProps = { ownerScopeId: "org-shared-browser", sections: [], state: "ready", rooms: [base], participants: [], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onSelectSession: vi.fn(), sessionRows: toRows(sessions) };
  const titles = () => within(screen.getByRole("region", { name: "Sessions" })).getAllByRole("button", { name: /^Open / }).map((b) => b.getAttribute("aria-label")!.match(/(Live|Old) work/)![0]);
  const { unmount } = render(<RoomDirectory {...props} currentUserId="dana" />);
  await userEvent.click(screen.getByRole("button", { name: "Session actions for Old work" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Archive" }));
  expect(titles()).toEqual(["Live work"]);
  unmount();
  const other = render(<RoomDirectory {...props} currentUserId="sam" />);
  expect(titles()).toEqual(["Live work", "Old work"]);
  other.unmount();
  render(<RoomDirectory {...props} currentUserId="dana" />);
  expect(titles()).toEqual(["Live work"]);
});

it("opens the row menu on right-click", async () => {
  render(<RoomDirectory ownerScopeId="org-context" sections={[]} state="ready" rooms={[base]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()}
    onSelectSession={vi.fn()} sessionRows={toRows([{ session: makeSession("a", "Live work", "busy"), ownerName: "Alice", own: false, owner: aliceFace }])} />);
  fireEvent.contextMenu(screen.getByRole("button", { name: /^Open .*Live work/ }));
  expect(await screen.findByRole("menuitem", { name: "Archive" })).toBeInTheDocument();
});

it("collapses the Sessions section through the shared prefs and keeps a count", async () => {
  const session = { sessionId: "s1", deviceId: "d", humanUserId: "me", provider: "codex" as const, providerVersion: "0.149.0", generation: "g", status: "idle" as const, activeTurnId: null,
    capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true, controlUnavailableReason: null, latestEventSeq: 0 };
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} sessionRows={toRows([{ session, ownerName: "Me", own: true, owner: { kind: "human" as const, id: "me", displayName: "Me", mentionHandle: null, role: "member" as const, listenMode: null, joinedAt: "", joinedSeq: 0 } }], "me")} />);
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
    await userEvent.click(screen.getByRole("button", { name: `Actions for ${label}` }));
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

it("ends the list with an Invite someone row that opens the invite dialog, and no separate lone-founder nudge", async () => {
  openInviteDialog.mockClear();
  const onNavigate = vi.fn();
  render(<RoomDirectory ownerScopeId="org" sections={[]} state="ready" rooms={[base]} participants={[atlas]}
    onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onNavigate={onNavigate} />);
  expect(screen.queryByText(/just you and your AI employees/)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Invite someone" }));
  expect(openInviteDialog).toHaveBeenCalledTimes(1);
  // Closes the phone drawer, like the rail's Invite.
  expect(onNavigate).toHaveBeenCalledTimes(1);
});

