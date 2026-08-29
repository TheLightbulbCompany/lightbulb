// sessions.list projection (sessions contract §3) — port of the web's
// useAgentSessions filtering, minus SWR: the mobile sheet fetches on open.

import { SESSION_ID_RE } from "@lightbulb/shared";

export interface AgentSession {
  /** Trailing UUID for a real session; null == the legacy "General" session. */
  sessionId: string | null;
  /** The full OpenClaw session key (as returned — lowercased by OpenClaw). */
  key: string;
  /** Display label — truncated to 48 chars. */
  label: string;
  updatedAt?: number;
  /** Last-message preview line (1-line, muted). */
  preview?: string;
}

interface RawSessionRow {
  key?: string;
  label?: string;
  derivedTitle?: string;
  title?: string;
  lastMessage?: { text?: string };
  updatedAt?: number;
}

/** Contract §3: the exact sessions.list params. */
export const SESSIONS_LIST_PARAMS = {
  includeGlobal: false,
  includeUnknown: false,
  includeDerivedTitles: true,
  includeLastMessage: true,
  limit: 50,
} as const;

function labelFor(row: RawSessionRow, sessionId: string): string {
  // A user-set label (sessions.patch {label}) always wins over derived titles.
  const raw = (row.label || row.derivedTitle || row.title || row.lastMessage?.text || "").trim();
  // Never blank — fall back to a short id so the row is always tappable.
  const rawLabel = raw || `Session ${sessionId.slice(0, 8)}`;
  return rawLabel.length > 48 ? `${rawLabel.slice(0, 48)}…` : rawLabel;
}

/**
 * Filter a sessions.list payload down to this agent+user's sessions:
 * keep only 4-part `agent:<agentId>:<userId>:<uuid>` rows — comparing the
 * agent/user segments case-insensitively, because OpenClaw returns lowercased
 * keys while Clerk user ids are mixed-case — sorted newest first, with the
 * "General" (legacy 3-part) session unshifted as the first row.
 */
export function projectSessionRows(
  payload: unknown,
  agentId: string,
  userId: string,
): AgentSession[] {
  const rows: RawSessionRow[] = Array.isArray(payload)
    ? (payload as RawSessionRow[])
    : ((payload as { sessions?: RawSessionRow[] } | null)?.sessions ?? []);
  const agentIdL = agentId.toLowerCase();
  const userIdL = userId.toLowerCase();

  const out: AgentSession[] = [];
  for (const row of rows) {
    if (typeof row?.key !== "string") continue;
    const parts = row.key.split(":");
    if (parts.length !== 4) continue;
    if (
      parts[0] !== "agent" ||
      parts[1].toLowerCase() !== agentIdL ||
      parts[2].toLowerCase() !== userIdL
    )
      continue;
    const sessionId = parts[3];
    if (!SESSION_ID_RE.test(sessionId)) continue;
    out.push({
      sessionId,
      key: row.key,
      label: labelFor(row, sessionId),
      updatedAt: row.updatedAt,
      preview: row.lastMessage?.text?.trim() || undefined,
    });
  }
  out.sort((a, b) => (b.updatedAt ?? 0) - (a.updatedAt ?? 0));

  // General is always the first row on mobile (it's the default session even
  // before any 3-part transcript exists); the legacy row, when present,
  // supplies its preview/updatedAt.
  const legacyKey = `agent:${agentIdL}:${userIdL}`;
  const generalRow = rows.find(
    (row) => typeof row?.key === "string" && row.key.toLowerCase() === legacyKey,
  );
  out.unshift({
    sessionId: null,
    key: generalRow?.key ?? legacyKey,
    label: "General",
    updatedAt: generalRow?.updatedAt,
    preview: generalRow?.lastMessage?.text?.trim() || undefined,
  });
  return out;
}

/**
 * Compact relative timestamp for session rows ("now", "5m", "3h", "2d",
 * "Jun 4"). Accepts seconds or milliseconds since epoch.
 */
export function relativeTime(ts: number | undefined, now: number = Date.now()): string {
  if (!ts || ts <= 0) return "";
  const ms = ts < 1e12 ? ts * 1000 : ts;
  const delta = Math.max(0, now - ms);
  const min = Math.floor(delta / 60_000);
  if (min < 1) return "now";
  if (min < 60) return `${min}m`;
  const hours = Math.floor(min / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  return new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
