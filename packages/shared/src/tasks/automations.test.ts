import { describe, expect, it } from "vitest";
import { canDecide, runNowReason, waitingWords } from "./automations";

const sam = { userId: "sam", admin: false };
const admin = { userId: "ana", admin: true };

describe("who turns an automation on (routers/automations.py)", () => {
  it("a rule goes by its match, not deliverTo", () => {
    const room = { deliverTo: "none", ownerUserId: "sam", match: { roomIds: ["r1"] } };
    expect(canDecide(room, sam)).toBe(false);
    expect(canDecide(room, admin)).toBe(true);
    expect(waitingWords(room)).toBe("Waiting on a workspace admin");
    const calendar = { deliverTo: "room:r1", ownerUserId: "sam", match: { sources: ["calendar"] } };
    expect(canDecide(calendar, sam)).toBe(true);
    expect(canDecide(calendar, admin)).toBe(false);
  });
  it("a schedule goes by where it posts", () => {
    expect(canDecide({ deliverTo: "room:r1", ownerUserId: "sam", match: null }, sam)).toBe(false);
    expect(canDecide({ deliverTo: "dm:sam", ownerUserId: "sam", match: null }, sam)).toBe(true);
  });
  it("Run now only while active", () => {
    expect(runNowReason({ status: "paused" }, true)).toBe("Resume it first");
    expect(runNowReason({ status: "paused_system" }, true)).toBe("Resume it first");
    expect(runNowReason({ status: "active" }, true)).toBeNull();
    expect(runNowReason({ status: "active" }, false)).toBe("Only its owner or an admin can run it");
  });
});
