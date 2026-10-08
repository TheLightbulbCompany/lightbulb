import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { type RoomDetail, type RoomMember } from "@lightbulb/shared";
import { Alert } from "react-native";

const mockPush = jest.fn();
jest.mock("expo-router", () => ({ router: { push: (href: string) => mockPush(href) } }));

import { RoomDetails } from "../RoomDetails";

const member = (over: Partial<RoomMember>): RoomMember => ({
  kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "member",
  listenMode: null, joinedAt: "x", joinedSeq: 0, ...over,
});
const friday = member({ kind: "agent", id: "agent-1", displayName: "Friday", mentionHandle: "friday", listenMode: "active" });
const bob = member({ id: "user-b", displayName: "Bob" });

const room = (over: Partial<RoomDetail> = {}, viewerRole: RoomMember["role"] = "member"): RoomDetail => ({
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 1, lastMessageAt: null, unreadCount: 0, updatedAt: "x", lastReadSeq: 0, createdAt: "x",
  members: [member({ role: viewerRole }), bob, friday],
  ...over,
});

async function setup(detail: RoomDetail, extra: Partial<Parameters<typeof RoomDetails>[0]> = {}) {
  const props = {
    room: detail,
    currentUserId: "user-a",
    candidates: [{ kind: "agent" as const, id: "agent-2", displayName: "Atlas" }],
    upsertMember: jest.fn(async () => detail),
    removeMember: jest.fn(async () => {}),
    leaveRoom: jest.fn(async () => {}),
    setArchived: jest.fn(async () => detail),
    ...extra,
  };
  await render(<RoomDetails {...props} />);
  return props;
}

/** Presses the destructive button of the Alert the last press raised. */
function confirmAlert(spy: jest.SpyInstance) {
  const buttons = spy.mock.calls.at(-1)?.[2] as { style?: string; onPress?: () => void }[];
  buttons.find((button) => button.style === "destructive")?.onPress?.();
}

describe("RoomDetails", () => {
  let alert: jest.SpyInstance;
  beforeEach(() => {
    alert = jest.spyOn(Alert, "alert").mockImplementation(() => {});
  });
  afterEach(() => alert.mockRestore());

  it("shows the room's name, kind and member count", async () => {
    await setup(room());
    expect(screen.getByText("#Launch")).toBeTruthy();
    expect(screen.getByText("Channel · 3 members")).toBeTruthy();
  });

  it("lets any member change an agent's mode, calling upsertMember", async () => {
    const props = await setup(room());
    await fireEvent.press(screen.getByText("Off"));
    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith({
      kind: "agent", id: "agent-1", role: "member", listenMode: "observe",
    }));
  });

  it("gives no member-level control in the room shared with Lightbulb", async () => {
    await setup(room({ shared: true }));
    expect(screen.queryByText("Off")).toBeNull();
    expect(screen.getByText("Listening")).toBeTruthy();
  });

  it("offers only what a session's chat allows: agents in and out, no archive, no people by hand", async () => {
    const carol = { kind: "human" as const, id: "user-c", displayName: "Carol" };
    await setup(room({ session: { sessionId: "s1" } }, "owner"), { candidates: [{ kind: "agent" as const, id: "agent-2", displayName: "Atlas" }, carol] });
    expect(screen.queryByText("Archive channel")).toBeNull();
    expect(screen.queryByText("Leave channel")).toBeNull();
    await fireEvent.press(screen.getByText("Add members"));
    expect(screen.getByLabelText("Atlas")).toBeTruthy();
    expect(screen.queryByLabelText("Carol")).toBeNull();
    await fireEvent.press(screen.getByText("Remove members"));
    expect(screen.getByLabelText("Friday")).toBeTruthy();
    expect(screen.queryByLabelText("Bob")).toBeNull();
  });

  it("hides the mode in a DM, where it is inert", async () => {
    await setup(room({ kind: "dm", name: null, members: [member({ role: "owner" }), friday] }));
    expect(screen.queryByText("Off")).toBeNull();
    expect(screen.queryByText("Listening")).toBeNull();
    expect(screen.queryByText("Mentions")).toBeNull();
  });

  it("lets a member leave a channel they do not own", async () => {
    const props = await setup(room());
    await fireEvent.press(screen.getByText("Leave channel"));
    await act(async () => confirmAlert(alert));
    await waitFor(() => expect(props.leaveRoom).toHaveBeenCalled());
  });

  it("offers no leave to the channel owner or in the shared room", async () => {
    await setup(room({}, "owner"));
    expect(screen.queryByText("Leave channel")).toBeNull();
    await screen.unmount();
    await setup(room({ shared: true }));
    expect(screen.queryByText("Leave channel")).toBeNull();
  });

  it("lets any member archive a group DM", async () => {
    const props = await setup(room({ kind: "group-dm", name: null }));
    await fireEvent.press(screen.getByText("Archive group chat"));
    await waitFor(() => expect(props.setArchived).toHaveBeenCalledWith(true));
    expect(screen.getByText("Leave group chat")).toBeTruthy();
  });

  it("opens the existing searchable picker immediately when requested by the header", async () => {
    await setup(room({}, "owner"), { initialAddPeopleOpen: true });
    expect(screen.getByPlaceholderText("Search people")).toBeTruthy();
    expect(screen.getByLabelText("Atlas")).toBeTruthy();
  });

  it("does not honor a direct-picker request from a member without permission", async () => {
    await setup(room({}, "member"), { initialAddPeopleOpen: true });
    expect(screen.queryByPlaceholderText("Search people")).toBeNull();
  });

  it("keeps adding and removing members to channel owners and admins", async () => {
    await setup(room());
    expect(screen.queryByText("Add members")).toBeNull();
    expect(screen.queryByText("Remove members")).toBeNull();
    await screen.unmount();

    const props = await setup(room({}, "admin"));
    await fireEvent.press(screen.getByText("Add members"));
    await fireEvent.press(screen.getByLabelText("Atlas"));
    // A newly added agent listens, on every plan.
    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith({
      kind: "agent", id: "agent-2", role: "member", listenMode: "active",
    }));

    await fireEvent.press(screen.getByText("Remove members"));
    await fireEvent.press(screen.getByLabelText("Bob"));
    await act(async () => confirmAlert(alert));
    await waitFor(() => expect(props.removeMember).toHaveBeenCalledWith({ kind: "human", id: "user-b" }));
  });

  it("lets any group chat member search for and add an eligible workspace person", async () => {
    const props = await setup(room({ kind: "group-dm", name: null }), {
      candidates: [
        { kind: "human", id: "user-c", displayName: "Cara" },
        { kind: "agent", id: "agent-2", displayName: "Atlas" },
      ],
    });

    await fireEvent.press(screen.getByText("Add people"));
    fireEvent.changeText(screen.getByPlaceholderText("Search people"), "atl");
    await waitFor(() => expect(screen.queryByLabelText("Cara")).toBeNull());
    await fireEvent.press(screen.getByLabelText("Atlas"));

    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith({
      kind: "agent", id: "agent-2", role: "member", listenMode: "active",
    }));
  });

  it("offers an email invite under Add people for someone not in the workspace yet", async () => {
    await setup(room({ kind: "group-dm", name: null }));
    await fireEvent.press(screen.getByText("Add people"));
    await fireEvent.press(screen.getByText("Not in the workspace yet? Invite them by email"));
    expect(mockPush).toHaveBeenCalledWith("/invite");
  });
});
