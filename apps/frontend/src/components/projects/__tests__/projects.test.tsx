// UX slice 6: a project's settings as a modal over it (name ▾ › Settings), its tile, Pause, Instructions; the sidebar's
// Pinned group and row; the empty Scheduled view's starter templates.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
vi.mock("@/components/huddle/useRoomHuddle", () => ({ useRoomHuddle: () => ({ userIds: [], available: false }) }));
import type { RoomSummary } from "@lightbulb/shared";
import { RoomHeader } from "@/components/messaging/RoomHeader";
import { RoomDirectoryRow } from "@/components/messaging/RoomDirectoryRow";
import { groupRooms } from "@/components/messaging/roomSections";
import type { DirectoryRoom } from "@/components/messaging/types";
import { pickTemplates } from "@/components/tasks/scheduleTemplates";

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

  it("Archive in the modal opens the header's confirm; a sidebar ⋯ › Settings opens it in one shot", async () => {
    const onSettingsOpened = vi.fn();
    render(header({ onSetArchived: vi.fn(), openSettings: true, onSettingsOpened }));
    const dialog = await screen.findByRole("dialog", { name: "Spring sale · Settings" });
    expect(onSettingsOpened).toHaveBeenCalledTimes(1);
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(await screen.findByRole("dialog", { name: "Archive Spring sale?" })).toBeInTheDocument();
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
});
