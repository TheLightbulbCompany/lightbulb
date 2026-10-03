import { describe, expect, it } from "vitest";
import { chunkInput, dropProbeReplies, MAX_INPUT_BYTES } from "../sessionInput";

describe("sessionInput", () => {
  it("drops xterm's probe replies and keeps typing", () => {
    for (const reply of ["\x1b[?1;2c", "\x1b[>0;276;0c", "\x1b[12;40R", "\x1b[0n", "\x1b[I", "\x1b[O", "\x1b]11;rgb:1616/1616/1a1a\x07", "\x1b]10;rgb:e6e6/e6e6/e9e9\x1b\\", "\x1b[?2026;2$y"]) {
      expect(dropProbeReplies(reply)).toBe("");
    }
    expect(dropProbeReplies("ab\x1b[?1;2cc")).toBe("abc");
    expect(dropProbeReplies("\x1b[A")).toBe("\x1b[A");
    expect(dropProbeReplies("\x1b\r")).toBe("\x1b\r");
  });
  it("chunks at the byte ceiling", () => {
    const parts = chunkInput(new Uint8Array(MAX_INPUT_BYTES * 2 + 1));
    expect(parts.map((p) => p.length)).toEqual([MAX_INPUT_BYTES, MAX_INPUT_BYTES, 1]);
  });
});
