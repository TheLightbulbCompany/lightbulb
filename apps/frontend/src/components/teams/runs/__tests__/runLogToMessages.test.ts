import { describe, it, expect } from "vitest";
import { pairToolBlocks } from "@/hooks/internal/toolBlockExtract";
import { resolveMessageBlocks } from "@lightbulb/shared";
import type { ContentBlock } from "@lightbulb/shared";
import { isAdapterNotice, runLogToMessages } from "../runLogToMessages";

/** Wrap an agent event the way the adapter writes it into the run log. */
function event(stream: string, data: unknown): string {
  return JSON.stringify({
    ts: "2026-07-27T00:00:00.000Z",
    stream: "stdout",
    chunk: `[openclaw-gateway:event] run=run-1 stream=${stream} data=${JSON.stringify(data)}\n`,
  });
}

function blocksOf(content: string): ContentBlock[] {
  const messages = runLogToMessages(content);
  return messages.length > 0 ? resolveMessageBlocks(messages[0]) : [];
}

describe("runLogToMessages", () => {
  it("returns nothing for empty content", () => {
    expect(runLogToMessages("")).toEqual([]);
  });

  it("coalesces assistant deltas into a single text block", () => {
    const blocks = blocksOf(
      [
        event("assistant", { delta: "Check" }),
        event("assistant", { delta: "ing the " }),
        event("assistant", { delta: "logs." }),
      ].join("\n"),
    );
    expect(blocks).toEqual([{ type: "text", text: "Checking the logs." }]);
  });

  it("lets an accumulated text event supersede the deltas it summarizes", () => {
    // Assistant events carry the full accumulated `text` alongside `delta`; a
    // text-only event must replace, not append, or the prose doubles.
    const blocks = blocksOf(
      [
        event("assistant", { delta: "Partial" }),
        event("assistant", { text: "Partial and final." }),
      ].join("\n"),
    );
    expect(blocks).toEqual([{ type: "text", text: "Partial and final." }]);
  });

  it("pairs a tool start with its result by toolCallId", () => {
    const blocks = blocksOf(
      [
        event("tool", { phase: "start", name: "bash", toolCallId: "t1", args: { cmd: "ls" } }),
        event("tool", { phase: "result", toolCallId: "t1", result: "a.txt" }),
      ].join("\n"),
    );

    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t1");
    expect(pairs[0].toolUseBlock.name).toBe("bash");
    expect(pairs[0].toolResultBlock?.content).toBe("a.txt");
  });

  it("marks a failed tool result as an error", () => {
    const blocks = blocksOf(
      [
        event("tool", { phase: "start", name: "bash", toolCallId: "t1" }),
        event("tool", { phase: "result", toolCallId: "t1", result: "boom", isError: true }),
      ].join("\n"),
    );
    expect(pairToolBlocks(blocks)[0].toolResultBlock?.isError).toBe(true);
  });

  it("attaches mid-execution progress to the running tool block", () => {
    const blocks = blocksOf(
      [
        event("tool", { phase: "start", name: "bash", toolCallId: "t1" }),
        event("tool", { phase: "update", toolCallId: "t1", partialResult: "line 1\n" }),
        event("command_output", { toolCallId: "t1", output: "line 2\n" }),
      ].join("\n"),
    );
    expect(blocks[0].progressText).toBe("line 1\nline 2\n");
  });

  it("keeps text interleaved with tools in emission order", () => {
    const blocks = blocksOf(
      [
        event("assistant", { delta: "First " }),
        event("tool", { phase: "start", name: "bash", toolCallId: "t1" }),
        event("tool", { phase: "result", toolCallId: "t1", result: "ok" }),
        event("assistant", { delta: "Then done." }),
      ].join("\n"),
    );
    expect(blocks.map((b) => b.type)).toEqual([
      "text",
      "tool_use",
      "tool_result",
      "text",
    ]);
    // The trailing prose opens a NEW text block rather than extending the first.
    expect(blocks[0].text).toBe("First ");
    expect(blocks[3].text).toBe("Then done.");
  });

  it("keeps a truncated data payload as text instead of dropping it", () => {
    // stringifyForLog clips `data` at 8000 chars, so big tool results arrive as
    // unparseable JSON. Losing the line entirely would hide the tool call.
    const clipped = JSON.stringify({
      ts: "2026-07-27T00:00:00.000Z",
      stream: "stdout",
      chunk: '[openclaw-gateway:event] run=run-1 stream=tool data={"phase":"result","result":"aaaa\n',
    });
    const blocks = blocksOf(clipped);
    expect(blocks).toHaveLength(1);
    expect(blocks[0].type).toBe("text");
    expect(blocks[0].text).toContain('"phase":"result"');
  });

  it("surfaces error and terminal lifecycle messages", () => {
    const blocks = blocksOf(
      [
        event("lifecycle", { phase: "start" }),
        event("error", { error: "provider refused" }),
        event("lifecycle", { phase: "failed", message: "run failed" }),
      ].join("\n"),
    );
    const text = blocks.map((b) => b.text ?? "").join("");
    expect(text).toContain("provider refused");
    expect(text).toContain("run failed");
    // lifecycle:start is noise the status field already covers.
    expect(text).not.toContain("start");
  });

  it("keeps non-event stdout lines as plain text", () => {
    const blocks = blocksOf(
      JSON.stringify({
        ts: "2026-07-27T00:00:00.000Z",
        stream: "stdout",
        chunk: "npm warn deprecated foo@1.0.0\n",
      }),
    );
    expect(blocks[0].text).toContain("npm warn deprecated");
  });

  it("strips the adapter-notice prefix from adapter lines", () => {
    const blocks = blocksOf(
      JSON.stringify({
        ts: "2026-07-27T00:00:00.000Z",
        stream: "stdout",
        chunk: "[openclaw-gateway] agent accepted runId=abc status=running\n",
      }),
    );
    expect(blocks[0].text).toBe("agent accepted runId=abc status=running");
  });

  it("treats the hermes-gateway lines as adapter notices", () => {
    expect(isAdapterNotice("[hermes-gateway] event stream HTTP 404; falling back to polling")).toBe(true);
    expect(isAdapterNotice("[hermes-gateway] status poll failed: 503")).toBe(true);
    expect(isAdapterNotice("[openclaw-gateway] agent accepted")).toBe(true);
    expect(isAdapterNotice("Traceback (most recent call last):")).toBe(false);
    const blocks = blocksOf(
      JSON.stringify({
        ts: "t",
        stream: "stderr",
        chunk: "[hermes-gateway] event stream HTTP 404; falling back to polling\n",
      }),
    );
    expect(blocks[0].text).toBe("event stream HTTP 404; falling back to polling");
  });

  it("keeps adapter notices out of the agent's prose block", () => {
    // Appending a notice into the open text block made it read as the agent's
    // own words ("...status=running Looking at the logs").
    const blocks = blocksOf(
      [
        JSON.stringify({
          ts: "t",
          stream: "stdout",
          chunk: "[openclaw-gateway] agent accepted runId=run-1 status=running\n",
        }),
        event("assistant", { delta: "Looking at the logs." }),
      ].join("\n"),
    );
    expect(blocks).toHaveLength(2);
    expect(blocks[1].text).toBe("Looking at the logs.");
  });

  it("keeps an error out of the agent's prose block and flags the message", () => {
    const messages = runLogToMessages(
      [
        event("assistant", { delta: "Retrying now." }),
        event("error", { error: "gateway stream hiccup" }),
      ].join("\n"),
    );
    const blocks = resolveMessageBlocks(messages[0]);
    expect(blocks).toHaveLength(2);
    expect(blocks[0].text).toBe("Retrying now.");
    expect(blocks[1].text).toBe("Error: gateway stream hiccup");
    // errorCode + the "Error: " prefix are what select MessageList's error card.
    expect(messages[0].errorCode).toBe("run_error");
  });

  it("leaves errorCode unset when the run logged no error", () => {
    const messages = runLogToMessages(event("assistant", { delta: "All good." }));
    expect(messages[0].errorCode).toBeUndefined();
  });
});
