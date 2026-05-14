import { describe, it, expect } from "vitest";
import {
  isTextBlock,
  isThinkingBlock,
  isToolUseBlock,
  isToolResultBlock,
  resolveToolUseId,
  resolveToolArgs,
  pairToolBlocks,
} from "../internal/toolBlockExtract";

describe("block type predicates", () => {
  it("isTextBlock matches text / output_text / input_text", () => {
    expect(isTextBlock({ type: "text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "output_text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "input_text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "thinking", thinking: "x" })).toBe(false);
    expect(isTextBlock(null)).toBe(false);
  });

  it("isThinkingBlock matches thinking type only", () => {
    expect(isThinkingBlock({ type: "thinking" })).toBe(true);
    expect(isThinkingBlock({ type: "text" })).toBe(false);
  });

  it("isToolUseBlock matches all case variants", () => {
    expect(isToolUseBlock({ type: "tool_use" })).toBe(true);
    expect(isToolUseBlock({ type: "toolUse" })).toBe(true);
    expect(isToolUseBlock({ type: "tool_call" })).toBe(true);
    expect(isToolUseBlock({ type: "toolCall" })).toBe(true);
    expect(isToolUseBlock({ type: "ToolUse" })).toBe(true);
    expect(isToolUseBlock({ type: "text" })).toBe(false);
  });

  it("isToolResultBlock matches all case variants", () => {
    expect(isToolResultBlock({ type: "tool_result" })).toBe(true);
    expect(isToolResultBlock({ type: "toolResult" })).toBe(true);
    expect(isToolResultBlock({ type: "ToolResult" })).toBe(true);
    expect(isToolResultBlock({ type: "text" })).toBe(false);
  });
});

describe("resolveToolUseId", () => {
  it("prefers id, then tool_use_id, then toolUseId", () => {
    expect(resolveToolUseId({ id: "a" })).toBe("a");
    expect(resolveToolUseId({ tool_use_id: "b" })).toBe("b");
    expect(resolveToolUseId({ toolUseId: "c" })).toBe("c");
    expect(resolveToolUseId({ id: "a", tool_use_id: "b" })).toBe("a");
    expect(resolveToolUseId({})).toBeUndefined();
  });
});

describe("resolveToolArgs", () => {
  it("prefers args, then arguments, then input", () => {
    expect(resolveToolArgs({ args: { a: 1 } })).toEqual({ a: 1 });
    expect(resolveToolArgs({ arguments: { b: 2 } })).toEqual({ b: 2 });
    expect(resolveToolArgs({ input: { c: 3 } })).toEqual({ c: 3 });
    expect(resolveToolArgs({ args: { a: 1 }, input: { c: 3 } })).toEqual({ a: 1 });
    expect(resolveToolArgs({})).toBeUndefined();
  });
});

describe("pairToolBlocks", () => {
  it("pairs tool_use with tool_result by id within one message's content", () => {
    const blocks = [
      { type: "tool_use", id: "t1", name: "exec", input: { cmd: "ls" } },
      { type: "tool_result", tool_use_id: "t1", content: "output" },
    ];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t1");
    expect(pairs[0].toolUseBlock).toBe(blocks[0]);
    expect(pairs[0].toolResultBlock).toBe(blocks[1]);
  });

  it("pairs by camelCase toolUseId variant", () => {
    const blocks = [
      { type: "tool_use", id: "t2" },
      { type: "tool_result", toolUseId: "t2" },
    ];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t2");
  });

  it("returns an unpaired tool_use without a matching result", () => {
    const blocks = [{ type: "tool_use", id: "t3", name: "exec" }];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t3");
    expect(pairs[0].toolResultBlock).toBeUndefined();
  });

  it("skips orphan tool_result without a matching tool_use", () => {
    const blocks = [{ type: "tool_result", tool_use_id: "ghost", content: "x" }];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(0);
  });
});
