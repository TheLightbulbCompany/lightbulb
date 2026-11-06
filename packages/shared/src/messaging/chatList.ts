/**
 * The phone's Chats list: every room the viewer is in and every live workspace
 * session, as ONE list sorted by last activity, plus the row's preview line and
 * the avatar's state ring. Pure: no React, no DOM, no react-native.
 */
import { sessionArchived, sessionArchivedByViewer, sessionStatus, sessionWaitingLine } from "./localSessionLabels";
import type { LocalSession, RoomSummary } from "./types";

export type ChatEntry =
  | { kind: "room"; key: string; at: number; room: RoomSummary }
  | { kind: "session"; key: string; at: number; session: LocalSession };

/** Web `.participant-avatar[data-agent-state]`. */
export type AvatarRing = "running" | "attention" | "paused";

/** Epoch ms, 0 for an absent or unparseable value (sorts last, never NaN). */
const epoch = (value: string | null | undefined): number => (value ? Date.parse(value) || 0 : 0);

export const roomActivityAt = (room: Pick<RoomSummary, "lastMessageAt" | "updatedAt">): number =>
  epoch(room.lastMessageAt) || epoch(room.updatedAt);

export const sessionActivityAt = (session: Pick<LocalSession, "lastEventAt" | "endedAt">): number =>
  epoch(session.lastEventAt) || epoch(session.endedAt);

export interface MergeChatsOptions {
  /** Sessions the viewer archived on this device → the `latestEventSeq` they had then. */
  archivedSessions?: Readonly<Record<string, number>>;
  now?: number;
}

/**
 * Rooms (minus deleted ones) and sessions — ended ones included, they are
 * ordinary chats (owner ruling 2026-09-26) — minus ended-on-purpose, three-day-offline and
 * locally archived ones — an archived session comes back once it has new
 * output), newest activity first; equal times order by key so the list never
 * shuffles between renders.
 */
export function mergeChats(
  rooms: readonly RoomSummary[],
  sessions: readonly LocalSession[],
  { archivedSessions = {}, now = Date.now() }: MergeChatsOptions = {},
): ChatEntry[] {
  const entries: ChatEntry[] = [];
  for (const room of rooms) {
    if (room.deletedAt) continue;
    entries.push({ kind: "room", key: `room:${room.roomId}`, at: roomActivityAt(room), room });
  }
  for (const session of sessions) {
    if (sessionArchived(session, now) || sessionArchivedByViewer(session, archivedSessions)) continue;
    entries.push({ kind: "session", key: `session:${session.sessionId}`, at: sessionActivityAt(session), session });
  }
  return entries.sort((a, b) => b.at - a.at || a.key.localeCompare(b.key));
}

/** "Leo: text" in a channel or group, "text" from a DM's counterpart, "You: text" for mine. */
export function roomPreview(
  room: Pick<RoomSummary, "kind" | "lastMessage">,
  viewerId: string | null | undefined,
): string {
  const last = room.lastMessage;
  if (!last) return "No messages yet";
  const body = plainPreview(last.textPreview) || (last.hasAttachments ? "Attachment" : "");
  const mine = last.authorKind === "human" && last.authorId === viewerId;
  if (mine) return `You: ${body}`;
  return room.kind === "dm" ? body : `${last.authorName}: ${body}`;
}

/** The web composer's escapes back to what was typed: `*_~` and backticks anywhere, `# > - + =` and "1." at a line
 *  start (backend `agent_text`). Other backslashes (`\\server\share`, `a\.b`) stay. Run after stripping marks that
 *  skip escaped characters, so a typed `x` keeps its backticks. */
export const unescapeTyped = (text: string): string =>
  text
    .replace(/\\([*_~`])/g, "$1")
    .replace(/^(\s*)\\([#>+=-])/gm, "$1$2")
    .replace(/^(\s*\d+)\\([.)])/gm, "$1$2");

/** A one-line list preview: markdown markers (bold `**`, code ticks, headings,
 * quotes, list bullets) dropped and whitespace collapsed. `__` is kept: it is
 * far likelier an identifier (`__init__.py`) than emphasis. */
export const plainPreview = (text: string): string =>
  unescapeTyped(text
    .replace(/\*\*|(?<!\\)`/g, "")
    .replace(/^\s{0,3}(#{1,6}|>|[-*+]|\d+\.)\s+/gm, ""))
    .replace(/\s+/g, " ")
    .trim();

/** What it waits on first (a prompt's reason, a job's hand-off note), else the last reply, else the status word. */
export function sessionPreview(session: Pick<LocalSession, "status" | "lastAssistantMessage" | "waitingFor" | "note">): string {
  return sessionWaitingLine(session) ?? (plainPreview(session.lastAssistantMessage ?? "") || sessionStatus[session.status]);
}

/**
 * Paperclip's agent status → ring. `active` (the created-but-never-run default;
 * the heartbeat moves an agent to `running` and back to `idle`) and `online`
 * deliberately draw NO ring: a green ring on every never-run agent would say
 * all six starters are working. (Web's `AGENT_STATUS_CLASS` counts them as
 * live; this is a phone-side choice, web is unchanged.)
 */
const RING_BY_STATUS: Readonly<Record<string, AvatarRing>> = {
  running: "running",
  thinking: "running",
  covered: "running",
  stalled: "running",
  error: "attention",
  failed: "attention",
  paused: "paused",
};

export const agentRingState = (status: string | null | undefined): AvatarRing | null =>
  (status ? RING_BY_STATUS[status.toLowerCase()] : undefined) ?? null;

export const sessionRingState = (session: Pick<LocalSession, "status" | "waitingFor" | "note" | "done">): AvatarRing | null =>
  sessionWaitingLine(session) !== null ? "attention" : session.status === "busy" && !session.done ? "running" : null;
