import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { RoomDirectory, type RoomDirectoryProps } from "../RoomDirectory";
import { SpecialistDmBanner } from "../SpecialistDmBanner";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const live = vi.hoisted(() => ({ superAgent: true }));
vi.mock("@/hooks/useLiveEvents", () => ({ useSuperAgent: () => live.superAgent }));
const openChatPanelWithDraft = vi.hoisted(() => vi.fn());
vi.mock("@/components/workspace/workspaceController", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/components/workspace/workspaceController")>(),
  openChatPanelWithDraft,
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

beforeEach(() => { live.superAgent = true; openChatPanelWithDraft.mockClear(); });

describe("sidebar with the super-agent on", () => {
  const NAMES = ["Lightbulb", "Alice", "Engineering", "Finance"];
  /** Row names in order (a row's text also carries the avatar initials and the Agent tag). */
  const rowsOf = (region: string) => within(screen.getByRole("region", { name: region })).getAllByRole("button", { name: /^(Lightbulb|Alice|Engineering|Finance)/ })
    .map((b) => NAMES.find((n) => b.textContent?.includes(n)));

  it("rosters people and only Lightbulb, pinned first; specialist DMs sit in a collapsed Specialists group", async () => {
    render(<RoomDirectory {...props} />);
    expect(rowsOf("Team")).toEqual(["Lightbulb", "Alice"]);
    const specialists = screen.getByRole("button", { name: "Expand Specialists" });
    expect(specialists).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /^Finance/ })).toBeNull();
    await userEvent.click(specialists);
    expect(screen.getByRole("button", { name: "Collapse Specialists" })).toBeInTheDocument();
    expect(rowsOf("Specialists")).toEqual(["Finance"]);
  });

  it("off: every agent is a roster row under Team and there is no Specialists group", () => {
    live.superAgent = false;
    render(<RoomDirectory {...props} />);
    expect(rowsOf("Team")).toEqual(["Lightbulb", "Alice", "Engineering", "Finance"]);
    expect(screen.queryByRole("region", { name: "Specialists" })).toBeNull();
  });
});

describe("SpecialistDmBanner", () => {
  it("names the specialist and the Ask link prefills the mention in the Lightbulb DM", async () => {
    render(<SpecialistDmBanner agentName="Finance" handle="finance" />);
    expect(screen.getByRole("note")).toHaveTextContent("Finance now works through Lightbulb. A message here starts a Finance thread.");
    await userEvent.click(screen.getByRole("button", { name: "Ask Finance in your Lightbulb DM ›" }));
    expect(openChatPanelWithDraft).toHaveBeenCalledWith("@finance ");
  });
});
