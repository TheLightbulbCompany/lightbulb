import type { RoomSummary } from "@lightbulb/shared/messaging";
import { findCeoDm } from "./phoneHome";

const room = (id: string, kind: RoomSummary["kind"], name: string): RoomSummary =>
  ({ id, kind, members: [{ id: name.toLowerCase(), displayName: name }] }) as RoomSummary;

it("selects the CEO DM instead of a workspace room", () => {
  expect(findCeoDm([room("general", "channel", "General"), room("ceo-room", "dm", "CEO")])?.id).toBe("ceo-room");
});

it("recognizes the default CEO teammate name", () => {
  expect(findCeoDm([room("friday-room", "dm", "Friday")])?.id).toBe("friday-room");
});