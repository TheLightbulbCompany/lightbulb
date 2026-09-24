import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type RoomDetail, type RoomMember } from "@lightbulb/shared";
import { Alert } from "react-native";

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

async function setup(detail: RoomDetail, subscribed: boolean | undefined, extra: Partial<Parameters<typeof RoomDetails>[0]> = {}) {
  const props = {
    room: detail,
    currentUserId: "user-a",
    subscribed,
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
    await setup(room(), true);
    expect(screen.getByText("#Launch")).toBeTruthy();
    expect(screen.getByText("Channel · 3 members")).toBeTruthy();
  });

  it("lets any member of a paid workspace change an agent's mode, calling upsertMember", async () => {
    const props = await setup(room(), true);
    await fireEvent.press(screen.getByText("Off"));
    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith({
      kind: "agent", id: "agent-1", role: "member", listenMode: "observe",
    }));
  });

  it("shows a free-tier member the mode read-only, a stored Listening as Mentions", async () => {
    await setup(room(), false);
    expect(screen.queryByText("Off")).toBeNull();
    expect(screen.getByText("Mentions")).toBeTruthy();
    expect(screen.queryByText("Listening")).toBeNull();
  });

  it("gives a free-tier admin the control with Listening locked as a paid option", async () => {
    const props = await setup(room({}, "admin"), false);
    const locked = screen.getByText("Listening (paid plan)");
    await fireEvent.press(locked);
    expect(props.upsertMember).not.toHaveBeenCalled();
    await fireEvent.press(screen.getByText("Off"));
    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith(expect.objectContaining({ listenMode: "observe" })));
  });

  it("gives no member-level control in the room shared with Lightbulb", async () => {
    await setup(room({ shared: true }), true);
    expect(screen.queryByText("Off")).toBeNull();
    expect(screen.getByText("Listening")).toBeTruthy();
  });

  it("hides the mode in a DM, where it is inert", async () => {
    await setup(room({ kind: "dm", name: null, members: [member({ role: "owner" }), friday] }), true);
    expect(screen.queryByText("Off")).toBeNull();
    expect(screen.queryByText("Listening")).toBeNull();
    expect(screen.queryByText("Mentions")).toBeNull();
  });

  it("shows a plain message when the server answers PLAN_REQUIRED", async () => {
    const upsertMember = jest.fn(async () => {
      throw new ApiError(403, { error: { code: "PLAN_REQUIRED", message: "x" } }, "API 403");
    });
    await setup(room({}, "admin"), true, { upsertMember });
    await fireEvent.press(screen.getByText("Off"));
    expect(await screen.findByText("Listening is part of the paid plan. Free-tier agents reply when mentioned.")).toBeTruthy();
  });

  it("lets a member leave a channel they do not own", async () => {
    const props = await setup(room(), true);
    await fireEvent.press(screen.getByText("Leave channel"));
    await act(async () => confirmAlert(alert));
    await waitFor(() => expect(props.leaveRoom).toHaveBeenCalled());
  });

  it("offers no leave to the channel owner or in the shared room", async () => {
    await setup(room({}, "owner"), true);
    expect(screen.queryByText("Leave channel")).toBeNull();
    await screen.unmount();
    await setup(room({ shared: true }), true);
    expect(screen.queryByText("Leave channel")).toBeNull();
  });

  it("lets any member archive a group DM", async () => {
    const props = await setup(room({ kind: "group-dm", name: null }), true);
    await fireEvent.press(screen.getByText("Archive group chat"));
    await waitFor(() => expect(props.setArchived).toHaveBeenCalledWith(true));
    expect(screen.getByText("Leave group chat")).toBeTruthy();
  });

  it("keeps adding and removing members to channel owners and admins", async () => {
    await setup(room(), true);
    expect(screen.queryByText("Add members")).toBeNull();
    expect(screen.queryByText("Remove members")).toBeNull();
    await screen.unmount();

    const props = await setup(room({}, "admin"), true);
    await fireEvent.press(screen.getByText("Add members"));
    await fireEvent.press(screen.getByLabelText("Atlas"));
    // Paid: a newly added agent listens.
    await waitFor(() => expect(props.upsertMember).toHaveBeenCalledWith({
      kind: "agent", id: "agent-2", role: "member", listenMode: "active",
    }));

    await fireEvent.press(screen.getByText("Remove members"));
    await fireEvent.press(screen.getByLabelText("Bob"));
    await act(async () => confirmAlert(alert));
    await waitFor(() => expect(props.removeMember).toHaveBeenCalledWith({ kind: "human", id: "user-b" }));
  });
});
