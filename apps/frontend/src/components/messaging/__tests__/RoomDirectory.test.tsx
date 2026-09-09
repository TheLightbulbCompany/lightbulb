import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RoomDirectory, type RoomDirectoryProps } from "../RoomDirectory";

const people = [
  { ref: "fixture-alice", name: "Alice Example", kind: "human" as const },
  { ref: "fixture-friday", name: "Friday Example", kind: "agent" as const },
];
const rooms = [
  { id: "opaque-private", name: "Leadership example", kind: "private" as const, unread: true, mentionCount: 2 },
  { id: "opaque-public", name: "General example", kind: "public" as const },
  { id: "opaque-dm", name: "Alice and Friday example", kind: "dm" as const },
];
function props(overrides: Partial<RoomDirectoryProps> = {}): RoomDirectoryProps {
  return { ownerScopeId: "synthetic-owner", rooms, participants: people, state: "ready", currentRoomId: "opaque-private", onSelectRoom: vi.fn(), ...overrides };
}

describe("authorized room directory", () => {
  it("shows authoritative audience, current selection and actionable counts, and selects the supplied opaque id", () => {
    const onSelectRoom = vi.fn();
    render(<RoomDirectory {...props({ onSelectRoom })} />);
    const room = screen.getByRole("button", { name: /Leadership example/ });
    expect(room).toHaveAttribute("aria-current", "page");
    expect(within(room).getByText("Private room")).toBeInTheDocument();
    expect(within(room).getByLabelText("2 mentions")).toBeInTheDocument();
    expect(within(room).getByLabelText("Unread")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Alice and Friday example/ }));
    expect(onSelectRoom).toHaveBeenCalledWith("opaque-dm");
  });

  it("creates only permitted room types and explicit participant-selected conversations", () => {
    const onCreate = vi.fn();
    render(<RoomDirectory {...props({ onCreate, permittedCreation: ["private", "conversation"] })} />);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    const type = screen.getByRole("combobox", { name: "Conversation type" });
    expect(within(type).queryByRole("option", { name: "Public room" })).not.toBeInTheDocument();
    fireEvent.change(type, { target: { value: "private" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Room name" }), { target: { value: "Planning example" } });
    fireEvent.click(screen.getByRole("button", { name: "Create room" }));
    expect(onCreate).toHaveBeenCalledWith({ kind: "private", name: "Planning example", participantRefs: [] });
  });

  it("searches the authorized people-and-agents picker and starts fresh history when adding to a DM", () => {
    const onNewConversation = vi.fn();
    render(<RoomDirectory {...props({ currentRoomId: "opaque-dm", newConversationFrom: { roomId: "opaque-dm", participantRefs: ["fixture-alice"] }, onNewConversation })} />);
    fireEvent.click(screen.getByRole("button", { name: "Add people or agents" }));
    expect(screen.getByText(/new conversation with fresh history/i)).toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox", { name: "Find people and agents" }), { target: { value: "Friday" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Friday Example.*Agent/ }));
    fireEvent.click(screen.getByRole("button", { name: "Start new conversation" }));
    expect(onNewConversation).toHaveBeenCalledWith({ participantRefs: ["fixture-alice", "fixture-friday"] });
    expect(screen.queryByText("Human")).not.toBeInTheDocument();
  });

  it("hides old names and actions on access removal and resets creation on owner turnover", () => {
    const base = props({ onCreate: vi.fn(), permittedCreation: ["public"] });
    const { rerender } = render(<RoomDirectory {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "New conversation" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Room name" }), { target: { value: "Sensitive draft" } });
    rerender(<RoomDirectory {...base} ownerScopeId="another-owner" rooms={[]} participants={[]} />);
    expect(screen.queryByDisplayValue("Sensitive draft")).not.toBeInTheDocument();
    rerender(<RoomDirectory {...base} state="access-removed" />);
    expect(screen.queryByText("Leadership example")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
