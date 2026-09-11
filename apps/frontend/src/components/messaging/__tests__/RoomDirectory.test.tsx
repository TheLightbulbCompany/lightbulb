import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import userEvent from "@testing-library/user-event";
import { RoomDirectory } from "../RoomDirectory";
import type { DirectoryParticipant, DirectoryRoom } from "../types";

const friday: DirectoryParticipant = { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: null, agentActivity: "running" };
const alice: DirectoryParticipant = { kind: "human", id: "alice", displayName: "Alice", mentionHandle: null };
const atlas: DirectoryParticipant = { kind: "agent", id: "atlas", displayName: "Atlas", mentionHandle: "atlas" };
const base: DirectoryRoom = { roomId: "channel", kind: "channel", name: "Launch", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null, unreadCount: 0, updatedAt: "2026-09-10T00:00:00Z", participants: [] };

it("creates through a centered portal with member settings and closes on success", async () => {
  const onCreate = vi.fn().mockResolvedValue(true);
  const { container } = render(<RoomDirectory ownerScopeId="org" state="ready" rooms={[]} participants={[alice, atlas]} onSelectRoom={vi.fn()} onSelectParticipant={vi.fn()} onCreate={onCreate} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  const dialog = screen.getByRole("dialog", { name: "Create a channel" });
  expect(container.contains(dialog)).toBe(false);
  fireEvent.change(within(dialog).getByLabelText("Channel name"), { target: { value: "Launch" } });
  fireEvent.click(within(dialog).getByLabelText("Select Atlas"));
  fireEvent.change(within(dialog).getByLabelText("Listen mode for Atlas"), { target: { value: "active" } });
  fireEvent.click(within(dialog).getByRole("button", { name: "Create channel" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(onCreate).toHaveBeenCalledWith({ kind: "channel", name: "Launch", members: [{ kind: "agent", id: "atlas", role: "member", listenMode: "active" }] });
});

it("preserves a failed draft and disables member controls while creating", async () => {
  const props = { ownerScopeId: "org", state: "ready" as const, rooms: [], participants: [atlas], onSelectRoom: vi.fn(), onSelectParticipant: vi.fn(), onCreate: vi.fn().mockResolvedValue(false) };
  const { rerender } = render(<RoomDirectory {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
  fireEvent.change(screen.getByLabelText("Channel name"), { target: { value: "Launch" } });
  fireEvent.click(screen.getByLabelText("Select Atlas"));
  rerender(<RoomDirectory {...props} creationBusy />);
  expect(screen.getByLabelText("Listen mode for Atlas")).toBeDisabled();
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

it("marks a conversation unread and navigates to the next unread conversation", async () => {
  const user = userEvent.setup();
  const onSelectRoom = vi.fn();
  const onMarkUnread = vi.fn().mockResolvedValue(undefined);
  render(<RoomDirectory ownerScopeId="org" state="ready" currentRoomId="channel" participants={[]}
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

it("shows a participant-selection error beside the open people directory", () => {
  render(<RoomDirectory ownerScopeId="org" state="ready" rooms={[]} participants={[alice]} onSelectRoom={vi.fn()}
    onSelectParticipant={vi.fn()} creationError="Conversation could not be opened." />);
  fireEvent.click(screen.getByRole("button", { name: "People and agents" }));
  expect(screen.getByRole("alert")).toHaveTextContent("Conversation could not be opened.");
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
