import { render, screen, within } from "@testing-library/react-native";
import type { RoomSummary } from "@lightbulb/shared";

import { RoomRow } from "../RoomRow";

const room = (over: Partial<RoomSummary>): RoomSummary => ({
  roomId: "r1", name: "launch", kind: "channel", roomRevision: 1, lastMessageSeq: 0, lastMessageAt: null,
  unreadCount: 0, updatedAt: "2026-09-14T00:00:00Z", topic: null, starred: false, muted: false, sectionId: null,
  members: [], lastReadSeq: 0, ...over,
});

it("tags a shared channel and nothing else", async () => {
  await render(<>
    <RoomRow room={room({ roomId: "ext", name: "lightbulb-acme", shared: true })} participants={[]} unread={0} muted={false} onPress={() => {}} />
    <RoomRow room={room({ roomId: "own" })} participants={[]} unread={0} muted={false} onPress={() => {}} />
  </>);
  expect(within(screen.getByTestId("room-row-ext")).getByText("Shared")).toBeTruthy();
  expect(within(screen.getByTestId("room-row-own")).queryByText("Shared")).toBeNull();
});
