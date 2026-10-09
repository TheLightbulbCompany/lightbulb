// apps/frontend/src/components/teams/runs/runLogToMessages.ts
//
// Fold a Paperclip heartbeat-run log into the same `HistoryMessage[]` shape
// `chat.history` returns, so the run-detail panel renders a run with the chat
// surface's own components instead of dumping raw text into a <pre>.
//
// The openclaw_gateway adapter logs EVERY agent event verbatim BEFORE it
// branches on the stream (paperclip-fork
// packages/adapters/openclaw-gateway/src/server/execute.ts) as
//
//   [openclaw-gateway:event] run=<id> stream=<name> data=<json>
//
// so the log already carries the structure the chat renderer wants: assistant
// deltas, tool start/result/update, command output. We fold those back into
// content blocks; `pairToolBlocks` then pairs tool_use with tool_result by id
// exactly as it does for a live chat transcript, and MessageList renders the
// blocks in array order.
//
// Deliberately NOT built on the backend's `_transform_agent_event`: that emits
// the LIVE frame vocabulary (tool_start/tool_end, append-deltas), and turning
// those back into bubbles needs the accumulator that lives inside
// useAgentChat's state machine. A finished run is history, so we target the
// history shape directly and reuse buildChatItems/groupBySide/MessageList
// unchanged.
"use client";

import type { ContentBlock, HistoryMessage } from "@lightbulb/shared";

// `data=` is matched with [\s\S]* so a payload containing newlines stays whole.
const EVENT_RE = /^\[openclaw-gateway:event\]\s+run=(\S+)\s+stream=(\S+)\s+data=([\s\S]*)$/;
// The retired OpenClaw adapter and the Hermes gateway adapter both log their own chatter
// ("event stream HTTP 404; falling back to polling") under a bracketed prefix.
const ADAPTER_NOTICE_PREFIXES = ["[openclaw-gateway]", "[hermes-gateway]"];

/** The adapter notice's prefix at the start of `line`, or null. */
function noticePrefix(line: string): string | null {
  const t = line.trim();
  return ADAPTER_NOTICE_PREFIXES.find((p) => t.startsWith(p)) ?? null;
}

/** The one adapter line that is benign noise: our facade has no events route, so every run falls back to polling. */
export function isBenignFallbackNotice(line: string): boolean {
  return /^\[hermes-gateway\] event stream HTTP \d+; falling back to polling/.test(line.trim());
}

export function isAdapterNotice(line: string): boolean {
  return noticePrefix(line) !== null;
}

function asRecord(v: unknown): Record<string, unknown> | null {
  if (typeof v !== "object" || v === null || Array.isArray(v)) return null;
  return v as Record<string, unknown>;
}

function asString(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/**
 * Accumulates content blocks for one run, keeping a pointer at the open text
 * block so consecutive assistant deltas coalesce into one paragraph instead of
 * one block per 150ms lump.
 */
class BlockBuilder {
  readonly blocks: ContentBlock[] = [];
  /** True once the log carried an error, so the message can opt into cards. */
  sawError = false;
  private openTextIndex: number | null = null;

  /** Append to the open text block, opening one if needed. */
  appendText(text: string): void {
    if (!text) return;
    if (this.openTextIndex === null) {
      this.openTextIndex = this.blocks.length;
      this.blocks.push({ type: "text", text });
      return;
    }
    const block = this.blocks[this.openTextIndex];
    block.text = (block.text ?? "") + text;
  }

  /**
   * Replace the open text block's body. Assistant events carry the ACCUMULATED
   * `text` alongside the incremental `delta`, so a text-only (or `replace`)
   * event supersedes what the deltas built rather than adding to it — the same
   * rule as the gateway's `_build_text_frame`.
   */
  setText(text: string): void {
    if (!text) return;
    if (this.openTextIndex === null) {
      this.appendText(text);
      return;
    }
    this.blocks[this.openTextIndex].text = text;
  }

  /** Push a non-text block and close the open text block. */
  push(block: ContentBlock): void {
    this.blocks.push(block);
    this.openTextIndex = null;
  }

  /**
   * Push a standalone text block that neither extends the open one nor gets
   * extended by the next delta. Adapter notices and error lines are NOT the
   * agent talking — appended into the prose block they read as the agent's own
   * words ("...status=running Looking at the failing check").
   */
  pushText(text: string): void {
    if (!text) return;
    this.blocks.push({ type: "text", text });
    this.openTextIndex = null;
  }

  /** The most recent tool_use block carrying `id`, for progress attachment. */
  findToolUse(id: string): ContentBlock | undefined {
    for (let i = this.blocks.length - 1; i >= 0; i--) {
      const b = this.blocks[i];
      if (b.type === "tool_use" && b.id === id) return b;
    }
    return undefined;
  }
}

/** Attach streaming mid-execution output to the matching live tool pill. */
function attachProgress(builder: BlockBuilder, id: string, text: string): void {
  if (!id || !text) return;
  const block = builder.findToolUse(id);
  if (!block) return;
  block.progressText = (block.progressText ?? "") + text;
}

/**
 * MessageList renders a text block as a typed error card when the message
 * carries an `errorCode` and the block opens with this prefix, so failures read
 * as failures instead of as more agent prose.
 */
const ERROR_PREFIX = "Error: ";

function handleEvent(builder: BlockBuilder, stream: string, raw: string): void {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw.trim());
  } catch {
    // `stringifyForLog(data, 8_000)` truncates large payloads mid-object, so
    // unparseable JSON is expected on big tool results. Keep the text rather
    // than dropping the line — a clipped result still tells you what happened.
    builder.appendText(raw.trim());
    return;
  }
  const data = asRecord(parsed) ?? {};

  if (stream === "assistant") {
    const delta = asString(data.delta);
    if (delta && data.replace !== true) {
      builder.appendText(delta);
    } else {
      builder.setText(asString(data.text));
    }
    return;
  }

  if (stream === "reasoning" || stream === "thinking") {
    const text = asString(data.delta) || asString(data.text);
    if (text) builder.push({ type: "thinking", thinking: text });
    return;
  }

  if (stream === "tool") {
    const phase = asString(data.phase);
    const toolCallId = asString(data.toolCallId);
    const name = asString(data.name);

    if (phase === "start" && name) {
      builder.push({
        type: "tool_use",
        // A tool_use with no resolvable id renders as permanently "running"
        // because no result can pair to it — synthesize one so the pair holds.
        id: toolCallId || `tool-${builder.blocks.length}`,
        name,
        input: data.args,
      });
      return;
    }

    if (phase === "update") {
      attachProgress(builder, toolCallId, asString(data.partialResult));
      return;
    }

    if (phase === "result" && toolCallId) {
      builder.push({
        type: "tool_result",
        tool_use_id: toolCallId,
        content: data.result,
        ...(data.isError ? { isError: true } : {}),
      });
    }
    return;
  }

  if (stream === "command_output") {
    attachProgress(builder, asString(data.toolCallId), asString(data.output));
    return;
  }

  if (stream === "error") {
    const message = asString(data.error) || asString(data.message);
    if (message) {
      builder.pushText(`${ERROR_PREFIX}${message}`);
      builder.sawError = true;
    }
    return;
  }

  if (stream === "lifecycle") {
    // Only surface terminal failures — start/end lifecycle noise adds nothing
    // the run's own status field doesn't already say.
    const phase = asString(data.phase).toLowerCase();
    if (phase === "error" || phase === "failed" || phase === "cancelled") {
      const message = asString(data.error) || asString(data.message);
      if (message) {
        builder.pushText(`${ERROR_PREFIX}${message}`);
        builder.sawError = true;
      }
    }
  }
}

/**
 * Parse the raw log `content` blob into history messages.
 *
 * Returns at most one assistant message: a run is one agent turn, and keeping
 * every block in a single `content` array is what lets `pairToolBlocks` (which
 * pairs WITHIN a message) match tool results to their calls.
 */
export function runLogToMessages(content: string): HistoryMessage[] {
  if (!content) return [];
  const builder = new BlockBuilder();

  for (const rawLine of content.split("\n")) {
    const trimmed = rawLine.trim();
    if (!trimmed) continue;

    // Log records are NDJSON `{ts, stream, chunk}`; a store that ever writes
    // plain lines still renders rather than dropping silently.
    let chunk = trimmed;
    if (trimmed.startsWith("{")) {
      try {
        const rec = JSON.parse(trimmed) as { chunk?: unknown };
        chunk = asString(rec.chunk);
      } catch {
        chunk = trimmed;
      }
    }
    if (!chunk.trim()) continue;

    const match = chunk.trim().match(EVENT_RE);
    if (match) {
      handleEvent(builder, asString(match[2]).toLowerCase(), asString(match[3]));
      continue;
    }
    const prefix = noticePrefix(chunk);
    if (prefix) {
      builder.pushText(chunk.trim().slice(prefix.length).trim());
      continue;
    }
    builder.appendText(`${chunk.replace(/\s+$/, "")}\n`);
  }

  if (builder.blocks.length === 0) return [];
  return [
    {
      role: "assistant",
      content: builder.blocks,
      // Opts the ERROR_PREFIX blocks into MessageList's typed error card. The
      // code is the generic one — the run's own errorCode is already rendered
      // by the panel's error banner, and this only selects a renderer.
      ...(builder.sawError ? { errorCode: "run_error" } : {}),
    },
  ];
}
