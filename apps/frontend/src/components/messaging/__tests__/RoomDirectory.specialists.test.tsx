import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RoomDirectory, type RoomDirectoryProps } from "../RoomDirectory";
import { SpecialistDmBanner } from "../SpecialistDmBanner";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const live = vi.hoisted(() => ({ superAgent: true }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent, useOptionalLiveEvents: () => null }));
const openAgentDmWithDraft = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/components/workspace/workspaceController")>(),
  openAgentDmWithDraft,
}));

const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const lightbulb: DirectoryParticipant = { kind: "agent", id: "agent_ceo", displayName: "Lightbulb", mentionHandle: "lightbulb" };
const finance: DirectoryParticipant = { kind: "agent", id: "agent_finance", displayName: "Finance", mentionHandle: "finance" };
const engineering: DirectoryParticipant = { kind: "agent", id: "agent_eng", displayName: "Engineering", mentionHandle: "engineering" };
const base: DirectoryRoom = { roomId: "dm-finance", kind: "dm", name: null, roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-11-04T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null, members: [], lastReadSeq: 0, participants: [finance] };
const props: RoomDirectoryProps = {
  ownerScopeId: "org", sections: [], state: "ready", rooms: [base], participants: [alice, lightbulb, finance, engineering], ceoAgentId: "agent_ceo",
  onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onCreate: vi.fn().mockResolvedValue(false),
};

beforeEach(() => { live.superAgent = true; openAgentDmWithDraft.mockClear(); });

describe("sidebar with the super-agent on", () => {
  it("W17-8: New chat lists Lightbulb first with its mark, and a group chat starts with it picked", async () => {
    render(<RoomDirectory {...props} onCreate={vi.fn().mockResolvedValue(true)} />);
    await userEvent.click(screen.getByRole("button", { name: "New channel or chat" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "New chat" }));
    const dialog = await screen.findByRole("dialog", { name: "New direct message" });
    const boxes = within(dialog).getAllByRole("checkbox");
    expect(boxes[0]).toHaveAccessibleName("Select Lightbulb");
    expect(boxes[0].closest(".room-choice")!.querySelector(".participant-avatar__mark")).not.toBeNull();
    expect(boxes[0]).not.toBeChecked();
    await userEvent.selectOptions(within(dialog).getByLabelText("Type"), "group-dm");
    expect(within(screen.getByRole("dialog", { name: "New group chat" })).getByRole("checkbox", { name: "Select Lightbulb" })).toBeChecked();
    // Anchored at the top: the dialog grows downward instead of re-centring.
    expect(screen.getByRole("dialog", { name: "New group chat" }).className).toMatch(/top-\[8dvh\]/);
  });

  it("+ › New channel opens the New channel dialog; + › New chat keeps DM and group chat, not Channel (UX slice 6)", async () => {
    const onCreate = vi.fn().mockResolvedValue(true);
    render(<RoomDirectory {...props} onCreate={onCreate} />);
    await userEvent.click(screen.getByRole("button", { name: "New channel or chat" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "New channel" }));
    const dialog = await screen.findByRole("dialog", { name: "New channel" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "Spring sale{Enter}");
    expect(onCreate).toHaveBeenCalledWith({ kind: "channel", name: "Spring sale", members: [], topic: null, icon: null, color: null });
    await userEvent.click(screen.getByRole("button", { name: "New channel or chat" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "New chat" }));
    const chat = await screen.findByRole("dialog", { name: "New direct message" });
    expect(within(chat).queryByRole("option", { name: "Channel" })).toBeNull();
  });

  const NAMES = ["Lightbulb", "Alice", "Engineering", "Finance"];
  /** Row names in order (a row's text also carries the avatar initials and the Agent tag). */
  const rowsOf = (region: string) => within(screen.getByRole("region", { name: region })).getAllByRole("button", { name: /^(Lightbulb|Alice|Engineering|Finance)/ })
    .map((b) => NAMES.find((n) => b.textContent?.includes(n)));

  it("rosters people and only Lightbulb, pinned first; specialists and their DMs live on the Team page, not here", () => {
    render(<RoomDirectory {...props} />);
    expect(rowsOf("People")).toEqual(["Lightbulb", "Alice"]);
    expect(screen.queryByRole("region", { name: "Specialists" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Specialists/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^(Finance|Engineering)/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Ask / })).toBeNull();
  });

  it("a specialist DM stays findable under Team while it has unread messages or is open, and hides once read", () => {
    const { rerender } = render(<RoomDirectory {...props} rooms={[{ ...base, unreadCount: 2 }]} />);
    expect(rowsOf("People")).toContain("Finance");
    rerender(<RoomDirectory {...props} rooms={[base]} currentRoomId="dm-finance" />);
    expect(rowsOf("People")).toContain("Finance");
    rerender(<RoomDirectory {...props} rooms={[base]} currentRoomId={undefined} />);
    expect(rowsOf("People")).not.toContain("Finance");
  });

  it("More carries what it hides: a shared channel's unreads are counted on its heading and open it", () => {
    const shared: DirectoryRoom = { ...base, roomId: "ext", kind: "channel", name: "partners", shared: true, participants: [alice], unreadCount: 3 };
    const { container, rerender } = render(<RoomDirectory {...props} rooms={[shared]} />);
    const more = container.querySelector("details.room-directory-more")!;
    expect(more).toHaveAttribute("open");
    expect(more.querySelector("summary")).toHaveAttribute("aria-label", "More, 3 unread or waiting");
    // The count is back (founder, 6 Nov 2026: Lightbulb stays a communications platform).
    expect(more.querySelector("summary .room-count")).toHaveTextContent("3");
    rerender(<RoomDirectory {...props} rooms={[{ ...shared, unreadCount: 0 }]} />);
    expect(container.querySelector("details.room-directory-more summary .room-count")).toBeNull();
  });

  it("an unknown front agent (roster not resolved, or no ceo role) keeps today's roster", () => {
    render(<RoomDirectory {...props} ceoAgentId={undefined} />);
    // Today's roster: every agent, alphabetical (nothing to pin without a known CEO).
    expect(rowsOf("People")).toEqual(["Alice", "Engineering", "Finance", "Lightbulb"]);
    expect(screen.queryByRole("region", { name: "Specialists" })).toBeNull();
  });

  it("off: every agent is a roster row under Team and there is no Specialists group", () => {
    live.superAgent = false;
    render(<RoomDirectory {...props} />);
    expect(rowsOf("People")).toEqual(["Lightbulb", "Alice", "Engineering", "Finance"]);
    expect(screen.queryByRole("region", { name: "Specialists" })).toBeNull();
  });
});

describe("SpecialistDmBanner", () => {
  it("names the specialist and the Ask link opens the front agent's DM with the mention drafted", async () => {
    render(<SpecialistDmBanner agentName="Finance" handle="finance" frontAgentId="agent_ceo" />);
    expect(screen.getByRole("note")).toHaveTextContent("To ask Finance for something, mention @finance in Lightbulb's chat.");
    await userEvent.click(screen.getByRole("button", { name: "Ask Finance ›" }));
    expect(openAgentDmWithDraft).toHaveBeenCalledWith("agent_ceo", "@finance ");
  });

  it("without a handle, nothing is invented: the DM opens with an empty draft", async () => {
    render(<SpecialistDmBanner agentName="Finance" handle={null} frontAgentId="agent_ceo" />);
    await userEvent.click(screen.getByRole("button", { name: "Ask Finance ›" }));
    expect(openAgentDmWithDraft).toHaveBeenCalledWith("agent_ceo", "");
  });
});

describe("unread counts are back (founder, 6 Nov 2026)", () => {
  it("a channel row shows its unread count pill and, beside it, the needs-you warn dot", async () => {
    const { RoomDirectoryRow } = await import("../RoomDirectoryRow");
    const room: DirectoryRoom = { ...base, roomId: "p1", kind: "channel", name: "spring-sale", participants: [alice], unreadCount: 4 };
    const { container } = render(<ul><RoomDirectoryRow room={room} label="spring-sale" active={false} busy={false} sections={[]}
      onSelect={vi.fn()} onCopyLink={vi.fn()} needsYou={2} /></ul>);
    const row = screen.getByRole("button", { name: /^spring-sale/ });
    expect(row).toHaveAccessibleName(expect.stringContaining("2 need you"));
    expect(row).toHaveAccessibleName(expect.stringContaining("4 unread messages"));
    expect(container.querySelector(".room-count")).toHaveTextContent("4");
    expect(container.querySelector("[data-needs-you]")).not.toBeNull();
    expect(row).toHaveClass("font-semibold");
  });
});
