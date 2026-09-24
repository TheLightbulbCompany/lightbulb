// apps/frontend/src/components/teams/issues/task-chat/transcript-adapter.ts

// Ported from upstream Paperclip @ v2026.916.1-isol8.2 (MIT, (c) 2025 Paperclip AI):
//   - isTerminalRunStatus, flattenSelfTalk, transcriptToTaskChatItems
//       <- ui/src/components/task-chat/transcript-adapter.ts
//   - buildTranscript                  <- ui/src/adapters/transcript.ts
//   - parseHermesGatewayStdoutLine     <- packages/adapters/hermes/src/gateway/ui/parse-stdout.ts
//
// Part A keeps only the hermes_gateway path — the one adapter every Lightbulb
// agent runs on. That parser emits assistant / thinking / stderr / system /
// stdout entries, and upstream's transcriptToTaskChatItems renders only the
// first two ("init / stderr / stdout / system … carry no thread-visible
// content"), so the tool/diff/provider-activity/protocol branches are not
// ported. The thinking + assistant branches are verbatim.

import type { TaskChatItem } from "./task-chat-model";

export type TranscriptEntry =
  | { kind: "assistant"; ts: string; text: string; delta?: boolean; channel?: "progress" | "final" | "unknown"; itemId?: string }
  | { kind: "thinking"; ts: string; text: string; delta?: boolean; lifecycle?: "started" | "completed"; channel?: "summary" | "detail" | "unknown"; itemId?: string }
  | { kind: "stderr"; ts: string; text: string }
  | { kind: "system"; ts: string; text: string }
  | { kind: "stdout"; ts: string; text: string };

/** One persisted run-log record (`{ts, stream, chunk}` NDJSON). */
export interface RunLogChunk {
  ts: string;
  stream: "stdout" | "stderr" | "system";
  chunk: string;
}

const TERMINAL_STATUSES = new Set(["failed", "timed_out", "cancelled", "interrupted", "succeeded"]);

export function isTerminalRunStatus(status: string | undefined | null): boolean {
  return status != null && TERMINAL_STATUSES.has(status);
}

/**
 * Flatten markdown-ish interstitial text to one plain line for the live parent
 * row. Stream-safe: markers are stripped without requiring pairs.
 */
export function flattenSelfTalk(text: string): string {
  return text
    .replace(/```[a-z]*\n?/gi, " ")
    .replace(/`/g, "")
    .replace(/^#{1,6}\s+/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/!?\[([^\]]*)\]\(([^)]*)\)/g, "$1")
    .replace(/\*+/g, "")
    .replace(/__/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// ---- hermes_gateway stdout parser (verbatim) ----

/**
 * Strip ANSI escape sequences (CSI, OSC) from terminal text.
 * Same pattern used in claude-local adapter quota.ts.
 */
function stripAnsi(text: string): string {
  return text
    .replace(/\u001B\][^\u0007]*(?:\u0007|\u001B\\)/g, "")
    .replace(/\u001B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])/g, "");
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function extractDirectReasoningText(data: Record<string, unknown>): string {
  return (
    asString(data.reasoning).trim() ||
    asString(data.reasoning_text).trim() ||
    asString(data.thinking).trim() ||
    asString(data.text).trim() ||
    asString(data.summary).trim() ||
    asString(data.content).trim()
  );
}

function extractReasoningText(data: Record<string, unknown> | null): string {
  if (!data) return "";
  const direct = extractDirectReasoningText(data);
  if (direct) return stripAnsi(direct);
  const nested = asRecord(data.data) ?? asRecord(data.payload);
  const nestedDirect = nested ? extractDirectReasoningText(nested) : "";
  return nestedDirect ? stripAnsi(nestedDirect) : "";
}

export function parseHermesGatewayStdoutLine(line: string, ts: string): TranscriptEntry[] {
  const cleaned = stripAnsi(line);
  const trimmed = cleaned.trim();
  if (!trimmed) return [];

  const eventMatch = trimmed.match(/^\[hermes-gateway:event\]\s+run=([^\s]+)\s+event=([^\s]+)\s+data=(.*)$/s);
  if (eventMatch) {
    const eventName = eventMatch[2];
    const data = asRecord(safeJsonParse(eventMatch[3]));
    if (eventName === "message.delta") {
      const delta = asString(data?.delta) || asString(data?.text_delta);
      return delta ? [{ kind: "assistant", ts, text: stripAnsi(delta), delta: true }] : [];
    }
    if (eventName === "run.failed" || eventName === "run.error") {
      const message = asString(data?.error) || asString(data?.message) || "Hermes run failed";
      return [{ kind: "stderr", ts, text: message }];
    }
    if (eventName === "reasoning.available") {
      const reasoning = extractReasoningText(data);
      return [{ kind: "thinking", ts, text: reasoning || "Hermes reasoning available" }];
    }
    return [{ kind: "system", ts, text: `Hermes event: ${eventName}` }];
  }

  if (trimmed.startsWith("[hermes-gateway]")) {
    return [{ kind: "system", ts, text: trimmed.replace(/^\[hermes-gateway\]\s*/, "") }];
  }

  return [{ kind: "stdout", ts, text: cleaned }];
}

// ---- persisted log -> transcript ----

/** Parse the log read's `content` into records; a malformed row is skipped. */
export function parseRunLogContent(content: string): RunLogChunk[] {
  const chunks: RunLogChunk[] = [];
  for (const raw of content.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const rec = asRecord(safeJsonParse(line));
    if (!rec || typeof rec.chunk !== "string") continue;
    const stream = rec.stream === "stderr" || rec.stream === "system" ? rec.stream : "stdout";
    chunks.push({ ts: asString(rec.ts) || new Date(0).toISOString(), stream, chunk: rec.chunk });
  }
  return chunks;
}

/** Upstream buildTranscript, with the hermes parser bound and no path redaction. */
export function buildTranscript(chunks: RunLogChunk[]): TranscriptEntry[] {
  const entries: TranscriptEntry[] = [];
  let stdoutBuffer = "";
  for (const chunk of chunks) {
    if (chunk.stream === "stderr") {
      entries.push({ kind: "stderr", ts: chunk.ts, text: chunk.chunk });
      continue;
    }
    if (chunk.stream === "system") {
      entries.push({ kind: "system", ts: chunk.ts, text: chunk.chunk });
      continue;
    }
    const combined = stdoutBuffer + chunk.chunk;
    const lines = combined.split(/\r?\n/);
    stdoutBuffer = lines.pop() ?? "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (trimmed) entries.push(...parseHermesGatewayStdoutLine(trimmed, chunk.ts));
    }
  }
  const trailing = stdoutBuffer.trim();
  if (trailing) {
    const ts = chunks.length > 0 ? chunks[chunks.length - 1]!.ts : new Date().toISOString();
    entries.push(...parseHermesGatewayStdoutLine(trailing, ts));
  }
  return entries;
}

// ---- transcript -> task chat items (thinking + assistant branches verbatim) ----

/** "Thought for Ns" once a coalesced thinking group spans ≥1s. */
function thoughtDurationLabel(startTs: string | undefined, endTs: string): string | undefined {
  if (!startTs) return undefined;
  const start = Date.parse(startTs);
  const end = Date.parse(endTs);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return undefined;
  const secs = Math.round((end - start) / 1000);
  if (secs < 1) return undefined;
  return secs < 60 ? `Thought for ${secs}s` : `Thought for ${Math.floor(secs / 60)}m ${secs % 60}s`;
}

/** Append token deltas onto the open logical line while preserving real newlines. */
function appendThinkingText(lines: string[], text: string, delta: boolean | undefined) {
  const fragments = text.split("\n");
  if (!delta || lines.length === 0) {
    lines.push(...fragments);
    return;
  }
  lines[lines.length - 1] = `${lines[lines.length - 1] ?? ""}${fragments[0] ?? ""}`;
  lines.push(...fragments.slice(1));
}

export interface TranscriptAdapterOptions {
  runId: string;
  agentName?: string;
  running: boolean;
}

export function transcriptToTaskChatItems(
  entries: readonly TranscriptEntry[],
  { runId, agentName, running }: TranscriptAdapterOptions,
): TaskChatItem[] {
  const items: TaskChatItem[] = [];
  const thinkingStartTs = new Map<number, string>();
  let thinkingIndex = -1;
  let thinkingChannel: "summary" | "detail" | "unknown" | undefined;
  let thinkingItemId: string | undefined;
  let messageIndex = -1;
  let messageChannel: "progress" | "final" | "unknown" | undefined;
  let messageItemId: string | undefined;

  const finishThinking = () => {
    if (thinkingIndex >= 0) {
      const item = items[thinkingIndex];
      if (item?.kind === "thinking") item.streaming = false;
    }
    thinkingIndex = -1;
    thinkingChannel = undefined;
    thinkingItemId = undefined;
  };

  for (const [i, entry] of entries.entries()) {
    switch (entry.kind) {
      case "thinking": {
        const lifecycleOnly = !entry.text;
        const openThinking = thinkingIndex >= 0 ? items[thinkingIndex] : undefined;
        const continuesAnonymousLifecycle =
          entry.itemId === undefined &&
          thinkingItemId === undefined &&
          lifecycleOnly &&
          entry.lifecycle === "completed" &&
          openThinking?.kind === "thinking" &&
          openThinking.lifecycleOnly === true;
        const sameThinkingItem =
          thinkingIndex >= 0 &&
          thinkingChannel === entry.channel &&
          ((entry.itemId !== undefined && entry.itemId === thinkingItemId) ||
            (entry.itemId === undefined && thinkingItemId === undefined && entry.delta === true) ||
            continuesAnonymousLifecycle);
        if (sameThinkingItem) {
          const it = items[thinkingIndex];
          if (it.kind === "thinking") {
            if (entry.text) {
              appendThinkingText(it.lines, entry.text, entry.delta);
              it.lifecycleOnly = false;
            }
            it.transcriptIndex = i;
            const label = thoughtDurationLabel(thinkingStartTs.get(thinkingIndex), entry.ts);
            if (label) it.summaryLabel = label;
            if (entry.lifecycle === "completed") it.streaming = false;
          }
        } else {
          finishThinking();
          items.push({
            id: `${runId}:think:${i}`,
            kind: "thinking",
            lines: entry.text ? entry.text.split("\n") : [],
            streaming: running && entry.lifecycle !== "completed",
            collapsed: !running,
            channel: entry.channel,
            lifecycleOnly,
            transcriptIndex: i,
          });
          thinkingIndex = items.length - 1;
          thinkingChannel = entry.channel;
          thinkingItemId = entry.itemId;
          thinkingStartTs.set(thinkingIndex, entry.ts);
          messageIndex = -1;
          messageChannel = undefined;
          messageItemId = undefined;
        }
        if (entry.lifecycle === "completed") finishThinking();
        break;
      }
      case "assistant": {
        if (!entry.text) break;
        finishThinking();
        const channel = entry.channel;
        const sameMessage =
          messageIndex >= 0 &&
          messageChannel === channel &&
          ((entry.itemId !== undefined && entry.itemId === messageItemId) ||
            (entry.itemId === undefined && messageItemId === undefined && entry.delta === true));
        if (sameMessage) {
          const it = items[messageIndex];
          if (it.kind === "message") {
            it.text += entry.text;
            it.transcriptIndex = i;
          }
        } else {
          const atMs = Date.parse(entry.ts);
          items.push({
            id: `${runId}:msg:${i}`,
            kind: "message",
            author: "agent",
            authorName: agentName,
            text: entry.text,
            channel,
            streaming: running,
            interstitial: channel !== "final",
            atMs: Number.isFinite(atMs) ? atMs : undefined,
            transcriptIndex: i,
          });
          messageIndex = items.length - 1;
          messageChannel = channel;
          messageItemId = entry.itemId;
        }
        break;
      }
      // init / stderr / stdout / system / user and non-runner result entries
      // carry no thread-visible content (status is rendered separately).
      default:
        break;
    }
  }

  // Only the message still open at the transcript tail is streaming.
  if (running) {
    for (const [idx, it] of items.entries()) {
      if (it.kind === "message" && idx !== messageIndex) it.streaming = false;
    }
  }
  return items;
}
