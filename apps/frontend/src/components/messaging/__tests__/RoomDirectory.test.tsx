import { fireEvent, render, screen, within } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { RoomDirectory } from "../RoomDirectory";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const friday: DirectoryParticipant = { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: null, agentActivity: "running" };
const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const atlas: DirectoryParticipant = { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas" };
const base: DirectoryRoom = { roomId: "channel", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", participants: [] };

it("names canonical DMs from counterparts and preserves selection and unread counts", () => {
  const onSelectRoom = vi.fn();
  render(<RoomDirectory ownerScopeId="org" state="ready" currentRoomId="friday-dm" participants={[]} onSelectRoom={onSelectRoom} onSelectParticipant={vi.fn()} rooms={[
    base,
    { ...base, roomId: "friday-dm", kind: "dm", name: null, participants: [friday], unreadCount: 3 },
    { ...base, roomId: "group", kind: "group-dm", name: null, participants: [alice, atlas] },
  ]} />);
  expect(screen.getByRole("button", { name: /Launch/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Alice and Atlas/ })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /^Direct message$/ })).not.toBeInTheDocument();
  const row = screen.getByRole("button", { name: /Friday/ });
  expect(row).toHaveAttribute("aria-current", "page");
  expect(within(row).getByLabelText("3 unread messages")).toHaveTextContent("3");
  expect(within(row).getByRole("img", { name: "Friday" }).parentElement).toHaveAttribute("data-agent-state", "running");
  fireEvent.click(row);
  expect(onSelectRoom).toHaveBeenCalledWith("friday-dm");
});

it("identifies agents without handles in People and agents", () => {
  const onSelectParticipant = vi.fn();
  render(<RoomDirectory ownerScopeId="org" state="ready" rooms={[]} participants={[friday, alice]} onSelectRoom={vi.fn()} onSelectParticipant={onSelectParticipant} />);
  fireEvent.click(screen.getByRole("button", { name: "People and agents" }));
  expect(screen.getByText("Agent")).toBeInTheDocument();
  expect(screen.getAllByText("Person")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: /Friday.*Agent/ }));
  fireEvent.click(screen.getByRole("button", { name: /Alice.*Person/ }));
  expect(onSelectParticipant).toHaveBeenNthCalledWith(1, friday);
  expect(onSelectParticipant).toHaveBeenNthCalledWith(2, alice);
});

it("keeps available rooms visible with a detail recovery notice", () => {
  const onRetry = vi.fn();
  render(<RoomDirectory ownerScopeId="org" state="error" rooms={[
    base,
    { ...base, roomId: "friday-dm", kind: "dm", name: null, participants: [friday] },
  ]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onRetry={onRetry} />);

  expect(screen.getByRole("button", { name: /Launch/ })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Friday/ })).toBeInTheDocument();
  expect(screen.getByRole("alert")).toHaveTextContent("Some conversation details could not be loaded.");
  fireEvent.click(screen.getByRole("button", { name: "Try again" }));
  expect(onRetry).toHaveBeenCalledOnce();
});

it("keeps an empty directory error blocking", () => {
  render(<RoomDirectory ownerScopeId="org" state="error" rooms={[]} participants={[]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onRetry={vi.fn()} />);

  expect(screen.getByRole("status")).toHaveTextContent("Conversations could not be loaded.");
  expect(screen.queryByRole("button", { name: /Launch/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
});
