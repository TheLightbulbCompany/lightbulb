import type { RoomKind } from "./types";

/**
 * The one notification shape, Slack/Linear-style, for every banner and toast:
 *
 *     Pulse · #GTM Strategy          <- line 1: who · where
 *     Searching the web for "…"      <- line 2: what, one short plain line
 *
 * The backend composes the phone pushes in Python with the same rules
 * (messaging-projector `_render_push`, `paperclip_events`, the room run-failure
 * push); keep them in step.
 */

/** Line 2's budget: one glance, no wrap on a phone banner. */
export const NOTIFICATION_LINE_CHARS = 80;

/** Line 1: "Pulse · GTM Strategy", or just the name when there is no place. */
export function notificationTitle(who: string, where?: string | null): string {
  return where ? `${who} · ${where}` : who;
}

/** A room as line 1 names it: "gtm-strategy", "DM", a group's name. */
export function roomWhere(room: { kind: RoomKind; name: string | null } | null | undefined): string | null {
  if (!room) return null;
  if (room.kind === "dm") return "DM";
  if (room.kind === "channel") return room.name ? `#${room.name}` : "Channel";
  return room.name || "Group chat";
}

/** Line 2: the first non-empty line of `text`, list/heading marks dropped, clipped. */
export function notificationLine(text: string | null | undefined, max = NOTIFICATION_LINE_CHARS): string {
  const line = (text ?? "")
    .split("\n")
    .map((part) => part.replace(/^\s*(?:[#>]+|[-*+]|\d+\.)\s+/, "").replace(/\*\*|`/g, "").replace(/\s+/g, " ").trim())
    .find(Boolean) ?? "";
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
}

/** "wants to hire Designer" → "Wants to hire Designer". */
export function sentenceCase(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text;
}

// Paperclip's run errorCodes, in plain words. Mirrors `_RUN_FAILURE_REASONS` in
// apps/backend/routers/paperclip_events.py; keep the two in step.
const RUN_FAILURE_REASONS: Readonly<Record<string, string>> = {
  setup_failed: "the workspace couldn't start",
  process_lost: "the workspace restarted mid-run",
  timeout: "it ran out of time",
  hermes_gateway_auth_failed: "it couldn't reach the workspace",
  hermes_gateway_upstream_error: "the AI model didn't answer",
  hermes_gateway_run_failed: "the agent hit an error",
};

/** Line 2 of a failed run: "Couldn't finish: the workspace restarted mid-run". */
export function runFailureLine(errorCode?: string | null, error?: string | null): string {
  const known = errorCode && Object.prototype.hasOwnProperty.call(RUN_FAILURE_REASONS, errorCode)
    ? RUN_FAILURE_REASONS[errorCode] : null;
  return notificationLine(`Couldn't finish: ${known ?? (notificationLine(error) || "the run hit an error")}`);
}
