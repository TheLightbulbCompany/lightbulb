import { describe, expect, it } from "vitest";
import { paperclipUserIdOf } from "../assignees";

describe("paperclipUserIdOf", () => {
  it("maps the Clerk id to the Paperclip id the Me filter compares", () => {
    const members = [
      { userId: "user_clerk", paperclipUserId: "pc_1" },
      { userId: "user_other", paperclipUserId: "pc_2" },
    ];
    expect(paperclipUserIdOf(members, "user_clerk")).toBe("pc_1");
    expect(paperclipUserIdOf(members, "user_missing")).toBeNull();
    expect(paperclipUserIdOf(undefined, "user_clerk")).toBeNull();
  });
});
