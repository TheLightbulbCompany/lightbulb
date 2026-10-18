import { actorName } from "../actorName";
import type { CompanyMember, TeamsAgent } from "../teamsData";

const agents = [{ id: "a1", name: "Friday" }] as TeamsAgent[];
const members: CompanyMember[] = [
  { userId: "user_ada", paperclipUserId: "pc_ada", name: "Ada" },
  { userId: "user_bo", paperclipUserId: "pc_bo", name: null, email: "bo@x.co" },
];

it("resolves an agent by id", () => {
  expect(actorName({ agentId: "a1", agents, members })).toBe("Friday");
});
it("resolves a member by Clerk or Paperclip id, falling back to email", () => {
  expect(actorName({ userId: "pc_ada", members, currentUserId: "user_zed" })).toBe("Ada");
  expect(actorName({ userId: "user_ada", members })).toBe("Ada");
  expect(actorName({ userId: "pc_bo", members })).toBe("bo@x.co");
});
it("says You for the current user", () => {
  expect(actorName({ userId: "pc_ada", members, currentUserId: "user_ada" })).toBe("You");
});
it("says Someone when nothing matches", () => {
  expect(actorName({ agentId: "nope", userId: "pc_unknown", agents, members })).toBe("Someone");
  expect(actorName({})).toBe("Someone");
});
