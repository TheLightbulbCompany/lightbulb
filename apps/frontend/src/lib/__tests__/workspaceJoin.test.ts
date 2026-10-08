import { describe, expect, it } from "vitest";
import { joinLinkTarget } from "../workspaceJoin";

describe("joinLinkTarget", () => {
  it.each([
    ["https://thelightbulb.company/join/org_abc123#t0k3n", "/join/org_abc123#t0k3n"],
    ["  /join/org_abc123#t0k3n ", "/join/org_abc123#t0k3n"],
    ["https://dev.thelightbulb.company/join/org_abc123/#t0k3n", "/join/org_abc123#t0k3n"],
  ])("accepts %j", (pasted, target) => expect(joinLinkTarget(pasted)).toBe(target));
  it.each(["", "hello", "https://example.com/chat#x", "/join/org_abc123", "/join/org_abc123#", "/join/user_abc#tok"])(
    "refuses %j", (pasted) => expect(joinLinkTarget(pasted)).toBeNull());
});
