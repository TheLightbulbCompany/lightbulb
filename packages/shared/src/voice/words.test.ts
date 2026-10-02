import { describe, expect, it } from "vitest";
import { errorText, liveErrorText, liveJoinErrorCode, liveMarker, liveStatus } from "./words";

describe("voice words", () => {
  it("falls back for unknown and prototype keys", () => {
    expect(errorText("connection_lost", "x")).toBe("The call dropped.");
    expect(errorText("__proto__", "x")).toBe("x");
    expect(errorText(undefined, "x")).toBe("x");
  });

  it("reads voice_limit's reason", () => {
    expect(liveErrorText({ code: "voice_limit", reason: "credits_low" })).toBe("Add credits to keep calling.");
    expect(liveErrorText({ code: "nope" })).toBe("Live couldn't start.");
  });

  it.each([
    [undefined, "Following along"],
    [{ "lightbulb.status": "following" }, "Following along"],
    [{ "lightbulb.status": "listening", "lightbulb.speaker": "Aden" }, "Listening to Aden"],
    [{ "lightbulb.status": "thinking" }, "Thinking…"],
    [{ "lightbulb.status": "speaking" }, "Speaking"],
    [{ "lightbulb.status": "working", "lightbulb.working": "Gauge — checking Stripe" }, "Gauge is working: checking Stripe"],
  ])("liveStatus(%j)", (attrs, text) => {
    expect(liveStatus(attrs)).toBe(text);
  });

  it("names who is live", () => {
    expect(liveMarker(["Aden"])).toBe("🔴 Live — Aden");
    expect(liveMarker(["Aden", "Leo"])).toBe("🔴 Live — Aden and Leo");
    expect(liveMarker(["Aden", "Leo", "Sam"])).toBe("🔴 Live — Aden and 2 others");
  });

  it("maps join failures", () => {
    expect(liveJoinErrorCode({ name: "NotAllowedError" })).toBe("mic_denied");
    expect(liveJoinErrorCode({ status: 503 })).toBe("voice_unavailable");
    expect(liveJoinErrorCode({ name: "NotFoundError" })).toBe("no_mic");
    expect(liveJoinErrorCode({ status: 404, body: { code: "ROOM_NOT_FOUND" } })).toBe("room_not_found");
    expect(liveJoinErrorCode(new Error("x"))).toBe("connection_lost");
  });
});
