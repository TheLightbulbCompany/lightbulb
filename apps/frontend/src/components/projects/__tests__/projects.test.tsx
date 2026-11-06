// UX slice 6: a project's settings as a modal over it (name ▾ › Settings), its tile, Pause, Instructions; the sidebar's
// Pinned group and row; the empty Scheduled view's starter templates.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
const ctl = vi.hoisted(() => ({ openPanel: vi.fn() }));
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/components/workspace/workspaceController")>(), openPanel: ctl.openPanel,
}));
const toast = vi.hoisted(() => ({ notifyApiError: vi.fn() }));
vi.mock("@/lib/notify", async (importOriginal) => ({ ...await importOriginal<typeof import("@/lib/notify")>(), notifyApiError: toast.notifyApiError }));
import type { RoomSummary } from "@lightbulb/shared";
import { RoomHeader } from "@/components/messaging/RoomHeader";
import { RoomDirectoryRow } from "@/components/messaging/RoomDirectoryRow";
import { groupRooms } from "@/components/messaging/roomSections";
import type { DirectoryRoom } from "@/components/messaging/types";
import { goalTemplates, pickTemplates } from "@/components/tasks/scheduleTemplates";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const room: RoomSummary = {
  roomId: "room-1", name: "Spring sale", kind: "channel", roomRevision: 3, lastMessageSeq: 9,
  lastMessageAt: null, unreadCount: 0, updatedAt: "2026-11-06T10:00:00Z",
  topic: "Sell 200 loaves", starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0,
};
const header = (extra: Partial<Parameters<typeof RoomHeader>[0]> = {}) => <RoomHeader room={room} roomName="Spring sale" counterparts={[]} participantCount={1}
  canEditRoom canLeave actionable pinnedCount={0} search={<span />} onOpenDetails={vi.fn()} compact
  settingsPeople={<section><h4>Members</h4></section>} {...extra} />;
const openSettings = async () => {
  await userEvent.click(screen.getByRole("button", { name: /Spring sale/ }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Settings" }));
  return screen.findByRole("dialog", { name: "Spring sale · Settings" });
};

describe("project settings", () => {
  it("name ▾ › Settings opens the modal; Details, Add a topic, Archive and Delete leave the menu", async () => {
    render(header({ onSetTopic: vi.fn(), onSetArchived: vi.fn(), onDeleteRoom: vi.fn() }));
    await userEvent.click(screen.getByRole("button", { name: /Spring sale/ }));
    const menu = await screen.findByRole("menu");
    for (const gone of ["Details", "Add a topic", "Edit topic", "Archive project", "Delete project", "Star"]) {
      expect(within(menu).queryByRole("menuitem", { name: gone })).toBeNull();
    }
    await userEvent.click(within(menu).getByRole("menuitem", { name: "Settings" }));
    const dialog = await screen.findByRole("dialog", { name: "Spring sale · Settings" });
    expect(within(dialog).getByRole("button", { name: "General" })).toHaveAttribute("aria-current", "page");
    expect(within(dialog).getByText("Delete project")).toBeInTheDocument();
  });

  it("the tile saves at once; Pause and Resume patch the project; Esc closes it", async () => {
    const onPatchRoom = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(header({ onPatchRoom }));
    const dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Project icon and colour" }));
    await userEvent.click(await screen.findByRole("button", { name: "Rocket" }));
    await waitFor(() => expect(onPatchRoom).toHaveBeenCalledWith({ icon: "rocket", color: null }));
    await userEvent.keyboard("{Escape}"); // the picker
    await userEvent.click(within(dialog).getByRole("button", { name: "Pause" }));
    await waitFor(() => expect(onPatchRoom).toHaveBeenLastCalledWith({ paused: true }));
    rerender(header({ onPatchRoom, room: { ...room, pausedAt: "2026-11-06T10:01:00Z" } }));
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Resume" }));
    await waitFor(() => expect(onPatchRoom).toHaveBeenLastCalledWith({ paused: false }));
    expect(screen.getAllByText("Paused")).toHaveLength(2); // the header's tag and the row's label
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("Memory › Instructions saves on blur and counts to 2,000; People shows the Members content", async () => {
    const onSetTopic = vi.fn().mockResolvedValue(undefined);
    render(header({ onSetTopic }));
    const dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Memory" }));
    const field = within(dialog).getByLabelText("Instructions");
    expect(field).toHaveAttribute("maxLength", "2000");
    await userEvent.clear(field);
    await userEvent.type(field, "Always sign emails 'Sam from Acme'");
    expect(within(dialog).getByText("34 / 2,000")).toBeInTheDocument();
    await userEvent.tab();
    await waitFor(() => expect(onSetTopic).toHaveBeenCalledWith("Always sign emails 'Sam from Acme'"));
    expect(await within(dialog).findByText("Saved")).toBeInTheDocument();
    expect(within(dialog).queryByText(/What Lightbulb remembers/)).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: "People" }));
    expect(within(dialog).getByRole("heading", { name: "Members" })).toBeInTheDocument();
  });

  it("Memory lists what Lightbulb noted here; an admin forgets one, a member can't; empty says how to add (W15-5)", async () => {
    const fact = { messageId: "m-1", fact: "Classes cap at 12 kids.", notedAt: new Date().toISOString() };
    const memoryClient = { listRoomMemory: vi.fn().mockResolvedValue({ items: [fact] }), forgetRoomMemory: vi.fn().mockResolvedValue(undefined) };
    const { unmount } = render(header({ memoryClient }));
    let dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Memory" }));
    expect(within(dialog).getByText("What Lightbulb remembers here")).toBeInTheDocument();
    expect(await within(dialog).findByText("Classes cap at 12 kids.")).toBeInTheDocument();
    expect(memoryClient.listRoomMemory).toHaveBeenCalledWith("room-1");
    await userEvent.click(within(dialog).getByRole("button", { name: "Forget: Classes cap at 12 kids." }));
    expect(memoryClient.forgetRoomMemory).toHaveBeenCalledWith("room-1", "m-1");
    expect(await within(dialog).findByText(/Tell Lightbulb things about this project/)).toBeInTheDocument();
    unmount();

    render(header({ memoryClient: { ...memoryClient, listRoomMemory: vi.fn().mockResolvedValue({ items: [fact] }) }, canEditRoom: false }));
    dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Memory" }));
    expect(await within(dialog).findByText("Classes cap at 12 kids.")).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: /^Forget/ })).toBeNull();
  });

  it("a failed Forget puts the fact back and says so", async () => {
    const fact = { messageId: "m-1", fact: "We only use Caputo flour.", notedAt: new Date().toISOString() };
    render(header({ memoryClient: { listRoomMemory: vi.fn().mockResolvedValue({ items: [fact] }), forgetRoomMemory: vi.fn().mockRejectedValue(new Error("no")) } }));
    const dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Memory" }));
    await userEvent.click(await within(dialog).findByRole("button", { name: "Forget: We only use Caputo flour." }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("That couldn't be forgotten.");
    expect(within(dialog).getByText("We only use Caputo flour.")).toBeInTheDocument();
  });

  it("at desktop width the phone-only section select stays hidden and the body sits in the content column (W13-1)", async () => {
    // jsdom applies the real stylesheets but no @media block, which is the desktop layout. messaging.css's
    // `.room-input { display: block }` loads after projects.css and used to show the select at every width, pushing
    // the body into the 160 px nav column so Memory couldn't be clicked.
    const style = document.createElement("style");
    const css = (f: string) => readFileSync(resolve(__dirname, "../..", f), "utf8");
    // messaging.css whole trips jsdom's selector engine, so only its `.room-input` rule, after projects.css as in the app.
    style.textContent = `${css("projects/projects.css")}\n${css("messaging/messaging.css").match(/^\.room-input \{[^}]*\}/m)![0]}`;
    document.head.appendChild(style);
    try {
      render(header());
      const dialog = await openSettings();
      expect(getComputedStyle(within(dialog).getByLabelText("Section")).display).toBe("none");
      expect(getComputedStyle(dialog.querySelector(".project-settings__body")!).gridColumn).toBe("2");
    } finally { style.remove(); }
  });

  it("Archive in the modal opens the header's confirm; a sidebar ⋯ › Settings opens it in one shot", async () => {
    const onSettingsOpened = vi.fn();
    render(header({ onSetArchived: vi.fn(), openSettings: "sidebar", onSettingsOpened }));
    const dialog = await screen.findByRole("dialog", { name: "Spring sale · Settings" });
    expect(onSettingsOpened).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    const confirm = await screen.findByRole("dialog", { name: "Archive Spring sale?" });
    // Focus stays in the confirm it was handed to.
    await waitFor(() => expect(confirm).toContainElement(document.activeElement as HTMLElement));
  });

  it("Esc while typing still saves the Instructions; a failure is a toast", async () => {
    const onSetTopic = vi.fn().mockRejectedValue(new Error("Too long"));
    render(header({ onSetTopic }));
    const dialog = await openSettings();
    await userEvent.click(within(dialog).getByRole("button", { name: "Memory" }));
    const field = within(dialog).getByLabelText("Instructions");
    await userEvent.clear(field);
    await userEvent.type(field, "Ship by Friday");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onSetTopic).toHaveBeenCalledTimes(1);
    expect(onSetTopic).toHaveBeenCalledWith("Ship by Friday");
    await waitFor(() => expect(toast.notifyApiError).toHaveBeenCalledWith(expect.any(Error), "The instructions could not be saved."));
  });

  it("focus goes back where Settings was opened: name ▾, the sidebar row's ⋯, or Home's card ⋯", async () => {
    const { rerender } = render(<>{header()}<button type="button" className="room-directory-actions" data-room-id="room-1">row ⋯</button></>);
    await openSettings();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: /Spring sale/ })).toHaveFocus());

    rerender(<>{header({ openSettings: "sidebar", onSettingsOpened: () => rerender(<>{header()}<button type="button" className="room-directory-actions" data-room-id="room-1">row ⋯</button></>) })}<button type="button" className="room-directory-actions" data-room-id="room-1">row ⋯</button></>);
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(screen.getByRole("button", { name: "row ⋯" })).toHaveFocus());

    rerender(<>{header({ openSettings: "home", onSettingsOpened: () => rerender(<>{header()}<button type="button" className="home-projects__more" data-room-id="room-1">card ⋯</button></>) })}<button type="button" className="home-projects__more" data-room-id="room-1">card ⋯</button></>);
    await screen.findByRole("dialog");
    await userEvent.keyboard("{Escape}");
    expect(ctl.openPanel).toHaveBeenCalledWith("home");
    await waitFor(() => expect(screen.getByRole("button", { name: "card ⋯" })).toHaveFocus());
  });
});

describe("Pinned (sidebar)", () => {
  const dRoom = (extra: Partial<DirectoryRoom>): DirectoryRoom => ({ ...room, participants: [], ...extra } as DirectoryRoom);

  it("the starred group reads Pinned with the switch on, Starred without", () => {
    const rooms = [dRoom({ starred: true })];
    const nameOf = (r: DirectoryRoom) => r.name ?? "";
    expect(groupRooms(rooms, [], { sort: "recent", nameOf, pinned: true })[0].label).toBe("Pinned");
    expect(groupRooms(rooms, [], { sort: "recent", nameOf })[0].label).toBe("Starred");
  });

  it("a project row shows its tile, offers Pin and Settings", async () => {
    const onOpenSettings = vi.fn();
    const onToggleStar = vi.fn();
    render(<ul><RoomDirectoryRow room={dRoom({ icon: "rocket", color: "project-3" })} label="Spring sale" active={false} busy={false} sections={[]}
      onSelect={vi.fn()} onCopyLink={vi.fn()} project onOpenSettings={onOpenSettings} onToggleStar={onToggleStar} /></ul>);
    expect(document.querySelector('.project-tile[data-color="project-3"] svg')).not.toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Spring sale" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Pin" }));
    expect(onToggleStar).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Actions for Spring sale" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Settings" }));
    expect(onOpenSettings).toHaveBeenCalled();
  });
});

describe("starter schedules", () => {
  it("the first two roster specialists with a template, then Lightbulb's", () => {
    expect(pickTemplates(["Lightbulb", "Engineering", "Finance", "Customer Operations"]).map((t) => t.name))
      .toEqual(["Unanswered customer email", "Weekly cash summary", "Weekly project summary"]);
    expect(pickTemplates([]).map((t) => t.specialist)).toEqual(["Growth", "Customer Ops", "Lightbulb"]);
  });

  it("a project with a goal gets three built from it (W13-13); none without", () => {
    const cards = goalTemplates("Write a short autumn specials menu blurb for our cafe. Keep it warm.");
    expect(cards.map((t) => t.name)).toEqual(["Monday plan", "Friday wrap-up", "What others are doing"]);
    expect(cards.every((t) => t.draft.includes('"Write a short autumn specials menu blurb for our cafe"'))).toBe(true);
    expect(goalTemplates(null)).toEqual([]);
  });
});
