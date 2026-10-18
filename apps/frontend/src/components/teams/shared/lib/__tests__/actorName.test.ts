import { expect, it } from "vitest";
import { actorName } from "../actorName";

const members = [{ userId: "user_1", paperclipUserId: "pc-1", name: "Ada" }];

it("agent, member by either id, You, then Someone", () => {
  expect(actorName({ agentId: "a1", agents: [{ id: "a1", name: "Friday" }] })).toBe("Friday");
  expect(actorName({ userId: "user_1", members })).toBe("Ada");
  expect(actorName({ userId: "pc-1", members })).toBe("Ada");
  expect(actorName({ userId: "pc-1", members, currentUserId: "user_1" })).toBe("You");
  expect(actorName({ userId: "nobody", members })).toBe("Someone");
  expect(actorName({})).toBe("Someone");
});
