import { describe, expect, it } from "vitest";
import { canSendToSession, sessionStatus } from "./localSessionLabels";
import type { LocalSession } from "./types";

const base = { capabilities: { sendMode: "queue" }, controlUnavailableReason: null, capabilitiesVerified: true } as unknown as LocalSession;

describe("session labels", () => {
  it("labels ended sessions and refuses to send to them", () => {
    expect(sessionStatus.ended).toBe("Ended");
    expect(canSendToSession({ ...base, status: "ended" })).toBe(false);
    expect(canSendToSession({ ...base, status: "idle" })).toBe(true);
  });
});
