import { describe, expect, it } from "vitest";
import { chunkInput, dropMouseReports, dropProbeReplies, MAX_INPUT_BYTES } from "../sessionInput";

describe("sessionInput", () => {
  it("drops xterm's probe replies and keeps typing", () => {
    for (const reply of ["\x1b[?1;2c", "\x1b[>0;276;0c", "\x1b[12;40R", "\x1b[0n", "\x1b[I", "\x1b[O", "\x1b]11;rgb:1616/1616/1a1a\x07", "\x1b]10;rgb:e6e6/e6e6/e9e9\x1b\\", "\x1b[?2026;2$y"]) {
      expect(dropProbeReplies(reply)).toBe("");
    }
    expect(dropProbeReplies("ab\x1b[?1;2cc")).toBe("abc");
    expect(dropProbeReplies("\x1b[A")).toBe("\x1b[A");
    expect(dropProbeReplies("\x1b\r")).toBe("\x1b\r");
  });
  it("drops mouse reports and keeps keys", () => {
    for (const report of ["\x1b[<0;10;5M", "\x1b[<0;10;5m", "\x1b[<65;200;60M", "\x1b[M !!", "\x1b[M\u00a0\u0100\u0100", "\x1b[32;10;5M"]) {
      expect(dropMouseReports(report)).toBe("");
    }
    // Arrows, function keys, modified keys, Shift+Enter and a bracketed paste are typing.
    for (const key of ["\x1b[A", "\x1bOB", "\x1b[1;5C", "\x1b[15~", "\x1b[3;2~", "\x1b\r", "\x1b[200~M 12;3;4M\x1b[201~", "M"]) {
      expect(dropMouseReports(key)).toBe(key);
    }
  });
  it("chunks at the byte ceiling", () => {
    const parts = chunkInput(new Uint8Array(MAX_INPUT_BYTES * 2 + 1));
    expect(parts.map((p) => p.length)).toEqual([MAX_INPUT_BYTES, MAX_INPUT_BYTES, 1]);
  });
});
