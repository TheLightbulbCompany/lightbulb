import { describe, expect, it } from "vitest";
import { accumulateStreamText } from "./streamText";

describe("accumulateStreamText", () => {
  it("appends deltas to the existing buffer", () => {
    expect(accumulateStreamText("He", { content: "llo", append: true })).toBe("Hello");
  });

  it("starts a buffer from null on an append delta", () => {
    expect(accumulateStreamText(null, { content: "Hi", append: true })).toBe("Hi");
  });

  it("replaces the buffer on a full-text frame (legacy / resync / final)", () => {
    expect(accumulateStreamText("stale prefix", { content: "Hello world" })).toBe("Hello world");
  });

  it("treats append:false the same as absent (replacement)", () => {
    expect(accumulateStreamText("abc", { content: "xyz", append: false })).toBe("xyz");
  });
});
