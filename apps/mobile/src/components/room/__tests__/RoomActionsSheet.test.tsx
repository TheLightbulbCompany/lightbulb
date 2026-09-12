import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import type { RoomSummary } from "@lightbulb/shared";

// `mock`-prefixed because babel-plugin-jest-hoist rejects any other out-of-scope
// name a factory reaches for, and referenced lazily for the same reason the rest
// of this suite's siblings do: babel lowers the `const` to `var`.
const mockSetStringAsync = jest.fn(async (_text: string) => true);
jest.mock("expo-clipboard", () => ({ setStringAsync: (text: string) => mockSetStringAsync(text) }), {
  virtual: true,
});

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "room-1",
  name: "launch",
  kind: "channel",
  roomRevision: 1,
  lastMessageSeq: 3,
  lastMessageAt: "2026-09-12T10:00:00Z",
  unreadCount: 0,
  updatedAt: "2026-09-12T10:00:00Z",
  topic: null,
  starred: false,
  muted: false,
  sectionId: null,
  members: [],
  lastReadSeq: 0,
  ...over,
});

import { RoomActionsSheet } from "../RoomActionsSheet";

describe("RoomActionsSheet", () => {
  beforeEach(() => mockSetStringAsync.mockClear());

  it("offers Star and Mute (not Unstar/Unmute) on a plain room", async () => {
    await render(
      <RoomActionsSheet
        room={room({})}
        onClose={() => {}}
        onSetPrefs={async () => {}}
        onMarkUnread={() => {}}
      />,
    );
    expect(screen.getByText("Star conversation")).toBeTruthy();
    expect(screen.getByText("Mute conversation")).toBeTruthy();
    expect(screen.queryByText("Unstar conversation")).toBeNull();
    expect(screen.queryByText("Unmute conversation")).toBeNull();
  });

  it("flips to Unstar/Unmute on a starred, muted room", async () => {
    await render(
      <RoomActionsSheet
        room={room({ starred: true, muted: true })}
        onClose={() => {}}
        onSetPrefs={async () => {}}
        onMarkUnread={() => {}}
      />,
    );
    expect(screen.getByText("Unstar conversation")).toBeTruthy();
    expect(screen.getByText("Unmute conversation")).toBeTruthy();
    expect(screen.queryByText("Star conversation")).toBeNull();
    expect(screen.queryByText("Mute conversation")).toBeNull();
  });

  it("reports the inverted value and closes on a successful star", async () => {
    const onSetPrefs = jest.fn(async () => {});
    const onClose = jest.fn();
    await render(
      <RoomActionsSheet
        room={room({ starred: false })}
        onClose={onClose}
        onSetPrefs={onSetPrefs}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Star conversation"));
    expect(onSetPrefs).toHaveBeenCalledWith({ starred: true });
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("reports the inverted value on mute", async () => {
    const onSetPrefs = jest.fn(async () => {});
    await render(
      <RoomActionsSheet
        room={room({ muted: true })}
        onClose={() => {}}
        onSetPrefs={onSetPrefs}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Unmute conversation"));
    expect(onSetPrefs).toHaveBeenCalledWith({ muted: false });
  });

  it("shows the web's copy and keeps the sheet open when the star save fails", async () => {
    const onSetPrefs = jest.fn(async () => {
      throw new Error("offline");
    });
    const onClose = jest.fn();
    await render(
      <RoomActionsSheet
        room={room({})}
        onClose={onClose}
        onSetPrefs={onSetPrefs}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Star conversation"));
    await waitFor(() => expect(screen.getByText("The star could not be saved.")).toBeTruthy());
    expect(onClose).not.toHaveBeenCalled();
    // The row is still there to retry.
    expect(screen.getByText("Star conversation")).toBeTruthy();
  });

  it("shows the web's copy when the mute save fails", async () => {
    const onSetPrefs = jest.fn(async () => {
      throw new Error("offline");
    });
    await render(
      <RoomActionsSheet
        room={room({})}
        onClose={() => {}}
        onSetPrefs={onSetPrefs}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Mute conversation"));
    await waitFor(() =>
      expect(screen.getByText("The notification setting could not be saved.")).toBeTruthy(),
    );
  });

  it("marks unread and closes", async () => {
    const onMarkUnread = jest.fn();
    const onClose = jest.fn();
    await render(
      <RoomActionsSheet
        room={room({})}
        onClose={onClose}
        onSetPrefs={async () => {}}
        onMarkUnread={onMarkUnread}
      />,
    );
    await fireEvent.press(screen.getByText("Mark unread"));
    expect(onMarkUnread).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalled();
  });

  it("copies a workspace room permalink and closes", async () => {
    const onClose = jest.fn();
    await render(
      <RoomActionsSheet
        room={room({ roomId: "room-9" })}
        onClose={onClose}
        onSetPrefs={async () => {}}
        onMarkUnread={() => {}}
      />,
    );
    await fireEvent.press(screen.getByText("Copy link"));
    await waitFor(() =>
      expect(mockSetStringAsync).toHaveBeenCalledWith(
        expect.stringContaining("panel=chat&room=room-9"),
      ),
    );
    expect(onClose).toHaveBeenCalled();
  });
});
