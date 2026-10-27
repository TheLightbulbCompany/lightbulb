import type { LocalSession } from "./types";

const TOOL_NAMES: Record<string, string> = { claude: "Claude", codex: "Codex", cursor: "Cursor", grok: "Grok" };
/** The coding tool's display name; an unknown future provider shows its raw id. */
export const sessionToolName = (provider: string): string => TOOL_NAMES[provider] ?? provider;
const AGENT_NAMES: Record<string, string> = { claude: "Claude Code", codex: "Codex", cursor: "Cursor", grok: "Grok CLI" };
/** The coding agent's product name ("Claude Code", "Grok CLI"); an unknown provider shows its raw id. */
export const sessionAgentName = (provider: string): string => AGENT_NAMES[provider] ?? provider;
/** Every agent a Mac with sharing on mirrors (owner ruling 2026-10-04: every session is tracked). */
export const SESSION_AGENTS = "Claude Code, Codex, Cursor and Grok CLI";
/** A tool's title is the first prompt, and a prompt that opens with a pasted link made the title
 *  one long URL ("https://code.cl…"). Shown as the host and the words after it; a name the owner
 *  typed (`displayTitle`) is never changed. */
const hostFirst = (title: string | undefined): string | undefined => {
  const [, address, words] = /^https?:\/\/(\S+)\s*(.*)$/i.exec(title ?? "") ?? [];
  const host = address?.split(/[/?#]/)[0].split("@").pop();
  return host ? [host, words].filter(Boolean).join(" · ") : title;
};
/** The title without the tool prefix, for surfaces that show the tool as a logo. */
// Never an id: VoiceOver reads it aloud as gibberish. Untitled is "New chat", as Happy does: the
// folder is the row's second line, and a folder name as the title made "lightbulb.nosync" look
// like a conversation.
export const sessionTitle = (s: LocalSession): string =>
  s.displayTitle?.trim() || hostFirst(s.title?.trim()) || "New chat";
export const sessionLabel = (s: LocalSession) => `${sessionToolName(s.provider)} · ${sessionTitle(s)}`;

export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for input", unknown: "Online", offline: "Offline", ended: "Ended",
};

/**
 * The sidebar's one state per session (spec 2026-10-26 (e), "Lifecycle states and what the sidebar
 * shows"), shared by web and mobile. A parked origin, a superseded origin, a subagent and a spare
 * never reach a client as a row (the Mac and the server drop them), so they have no state here.
 *  - `here`: running in a terminal on its Mac (or a Lightbulb holder): Open.
 *  - `background`: a Claude background job (`claude attach`): Attach.
 *  - `waiting`: a prompt is open in its terminal, or its job left its person a hand-off note.
 *  - `done`: a background job whose turn is over; it may still sit at its shell.
 *  - `offline`: its Mac is not heard from.
 *  - `stopped`: it ended, not on purpose: Resume.
 *  - `hidden`: its person ended it on purpose (owner ruling 2026-10-08): no list shows it.
 */
export type SessionState = "here" | "background" | "waiting" | "done" | "offline" | "stopped" | "hidden";

export function sessionState(s: Pick<LocalSession, "status" | "endedOnPurpose" | "note" | "done" | "terminal">): SessionState {
  if (s.status === "ended") return s.endedOnPurpose ? "hidden" : "stopped";
  if (s.status === "offline") return "offline";
  if (s.status === "waiting-approval" || s.note) return "waiting";
  if (s.done) return "done";
  return s.terminal === "attach" ? "background" : "here";
}

/** Session notifications per kind (GET/PUT /users/me/prefs `sessionNotifications`). */
export type NotifyLevel = "off" | "badge" | "banner";
export type SessionNotifications = {
  waiting: NotifyLevel; finished: NotifyLevel; attention: NotifyLevel; onlyUnfocused: boolean; sound: boolean;
};
export const SESSION_NOTIFICATION_DEFAULTS: SessionNotifications = {
  waiting: "banner", finished: "banner", attention: "badge", onlyUnfocused: false, sound: true,
};

/** The Dock / tab count: your own sessions waiting on a prompt or holding a hand-off note, each
 *  counted unless that kind is off. */
export function sessionsNeedingYou(
  sessions: readonly Pick<LocalSession, "humanUserId" | "status" | "note">[],
  userId: string | null | undefined,
  prefs: SessionNotifications = SESSION_NOTIFICATION_DEFAULTS,
): number {
  return sessions.filter((s) => s.humanUserId === userId && (
    s.status === "waiting-approval" ? prefs.waiting !== "off"
      : s.status !== "ended" && s.status !== "offline" && Boolean(s.note?.trim()) && prefs.attention !== "off"
  )).length;
}

/** One label per state. A running one says what it does now (`sessionStatus`): "Working", "Idle". */
export const sessionStateLabel = (s: LocalSession): string => {
  const state = sessionState(s);
  return state === "here" ? sessionStatus[s.status]
    : { background: "Running in background", waiting: "Waiting on you", done: "Done", offline: "Offline", stopped: "Stopped", hidden: "Ended" }[state];
};

/** The state's primary action: Open its terminal, Attach to the job, Resume what stopped; none while its Mac is offline. */
export const sessionStateAction = (s: LocalSession): "Open" | "Attach" | "Resume" | null => {
  const state = sessionState(s);
  if (state === "stopped") return "Resume";
  if (state === "offline" || state === "hidden") return null;
  return s.terminal === "attach" ? "Attach" : "Open";
};

/** "3 files changed +12 −4", the Codex wording. */
export const changesLabel = (c: { files: number; additions: number; deletions: number }): string =>
  `${c.files} ${c.files === 1 ? "file" : "files"} changed +${c.additions} −${c.deletions}`;

/** "2 agents running"; null with none (spec 2026-10-26 (f) gap 16). */
export const sessionAgentsLabel = (s: Pick<LocalSession, "agents">): string | null =>
  s.agents ? `${s.agents} ${s.agents === 1 ? "agent" : "agents"} running` : null;

/** "PR #123 · checks passing", "PR #123 · merged"; null without a PR (gap 15). */
export const sessionPrLabel = (s: Pick<LocalSession, "pr">): string | null => {
  const pr = s.pr;
  if (!pr) return null;
  const what = pr.state !== "open" ? pr.state : !pr.checks ? "open" : `checks ${pr.checks === "pending" ? "running" : pr.checks}`;
  return `PR #${pr.number} · ${what}`;
};

/** A row's signals after its other line: running agents and the PR. Null with neither. */
export const sessionSignalsLine = (s: Pick<LocalSession, "agents" | "pr">): string | null =>
  [sessionAgentsLabel(s), sessionPrLabel(s)].filter(Boolean).join(" · ") || null;

/** What a waiting session waits on, as its row's preview: the open prompt's reason, else its job's
 *  note. A note is shown as text, never as a prompt. Null when it waits on nothing. */
export const sessionWaitingLine = (s: Pick<LocalSession, "status" | "waitingFor" | "note">): string | null =>
  s.status === "waiting-approval" ? `${sessionStatus["waiting-approval"]}${s.waitingFor ? `: ${s.waitingFor}` : ""}`
    : s.status === "ended" || s.status === "offline" ? null : s.note?.trim() || null;

/** Why an ended session ended, when it is not the ordinary leave. `mac` is "your Mac" or "Leo's Mac"; the phone passes its curly ’ as `apostrophe`.
 *  "stopped while idle": the Mac stopped it after an idle hour with nobody on it (sooner when short of memory), as Claude's background sessions; Resume continues it. */
export const endedWhy = (s: Pick<LocalSession, "status" | "endedReason">, mac: string, apostrophe = "'"): string | null => {
  if (s.status !== "ended") return null;
  if (s.endedReason === "login-session-ended") return `${mac}${apostrophe}s login session restarted`;
  return s.endedReason === "idle-stopped" ? "stopped while idle" : null;
};

export const unavailableReason: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string> = {
  // Owner ruling 2026-10-04: a person never reads "not verified" about their own session.
  "runtime-unverified": "Lightbulb can't send to this session. Use its terminal on the Mac; starting the session again there turns sending back on.",
  "endpoint-unavailable": "The native input connection is unavailable.",
  "unsupported-version": "This provider version does not support verified control.",
  "history-gap": "History has a gap. Control is unavailable until the desktop reconnects safely.",
};

/** Room targets require a live session; only direct composers opt into offline queuing. */
export const canSendToSession = (session: LocalSession, allowOffline = false): boolean =>
  session.status !== "ended" && session.capabilitiesVerified &&
  !session.controlUnavailableReason && session.capabilities.sendMode !== "none" &&
  (session.status !== "offline" || (allowOffline && session.capabilities.sendMode === "queue")) &&
  !(session.status === "busy" && session.capabilities.sendMode === "immediate" && !session.activeTurnId);

/** Why the owner cannot send right now, shared by web and mobile; null when sending is available. */
export const sendBlockedReason = (session: LocalSession): string | null =>
  canSendToSession(session, true) ? null
    : session.controlUnavailableReason ? unavailableReason[session.controlUnavailableReason]
      : session.status === "offline" ? "This session cannot queue a follow-up while the Mac is offline."
        : "This session does not accept remote input.";

/** The remount key for a shared session's panel/body: a fresh generation is a
 *  fresh mount, everywhere one is rendered. */
export const sessionViewKey = (session: Pick<LocalSession, "sessionId" | "generation">): string =>
  `${session.sessionId}:${session.generation}`;

export interface WorkspaceSection { key: string; title: string; data: LocalSession[] }

/** The sidebar's grouping, shared by web and mobile: one section per person, mine first; within a
 *  person newest activity first, as Happy orders its list (owner ruling 2026-10-13). Not by status:
 *  ranking "Working" first put a session the Mac wrongly thought busy above everything. */
export function workspaceSections(sessions: readonly LocalSession[], currentUserId: string | null | undefined, name: (humanUserId: string) => string): WorkspaceSection[] {
  const humans = [...new Set(sessions.map((s) => s.humanUserId))];
  const title = (human: string) => (human === currentUserId ? "You" : name(human));
  const at = (s: LocalSession) => Date.parse(s.lastEventAt ?? s.endedAt ?? "") || 0;
  humans.sort((a, b) => Number(b === currentUserId) - Number(a === currentUserId) || title(a).localeCompare(title(b)));
  return humans.map((human) => ({
    key: human, title: title(human),
    data: sessions.filter((s) => s.humanUserId === human).sort((a, b) => Number(sessionLive(b)) - Number(sessionLive(a))
      || at(b) - at(a) || a.sessionId.localeCompare(b.sessionId)),
  }));
}

/** Offline this long and a session stops counting: no unread dot, no "sharing" presence. Computed at read time, never stored. */
export const SESSION_ARCHIVE_AFTER_MS = 3 * 24 * 60 * 60 * 1000;

/** Offline, and last heard from at least `SESSION_ARCHIVE_AFTER_MS` ago. It stops being so the moment it reconnects. */
export const sessionArchived = (session: LocalSession, now = Date.now()): boolean =>
  session.status === "offline" && !!session.lastEventAt && now - Date.parse(session.lastEventAt) >= SESSION_ARCHIVE_AFTER_MS;

/** The desktop is mirroring it right now (working, waiting, idle, or activity unknown): the only sessions the sidebar lists. */
export const sessionLive = (session: LocalSession): boolean => session.status !== "offline" && session.status !== "ended";

/**
 * Its owner can end it from Lightbulb: live, and its process is its own (one Lightbulb holds, or a
 * Claude: a plain one, or a background session, which its Mac stops with `claude stop`). A Codex app or
 * daemon process hosts many conversations. `kind: "background"` is a legacy row no Mac registers any more.
 */
export const sessionEndable = (session: LocalSession): boolean => sessionLive(session)
  && (session.terminal === "held" || (session.provider === "claude" && session.kind !== "background"));

/** How many live sessions the sidebar shows before "+N more". */
export const SIDEBAR_SESSION_CAP = 5;

export interface SessionDayGroup { key: "today" | "yesterday" | "week" | "earlier"; title: string; data: LocalSession[] }

/** The "All sessions" list, shared by web and mobile: newest activity first, bucketed by the viewer's local day. */
export function sessionHistory(sessions: readonly LocalSession[], now = Date.now()): SessionDayGroup[] {
  const at = (s: LocalSession) => Date.parse(s.lastEventAt ?? s.endedAt ?? "") || 0;
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  const today = day.getTime();
  day.setDate(day.getDate() - 1);
  const yesterday = day.getTime();
  day.setDate(day.getDate() - 5);
  const week = day.getTime();
  const groups: SessionDayGroup[] = [
    { key: "today", title: "Today", data: [] }, { key: "yesterday", title: "Yesterday", data: [] },
    { key: "week", title: "This week", data: [] }, { key: "earlier", title: "Earlier", data: [] },
  ];
  for (const s of [...sessions].sort((a, b) => at(b) - at(a))) {
    const t = at(s);
    groups[t >= today ? 0 : t >= yesterday ? 1 : t >= week ? 2 : 3].data.push(s);
  }
  return groups.filter((group) => group.data.length > 0);
}

/** Per viewer, per session: the `latestEventSeq` last seen. Kept on the device (a convenience, not state). */
export type SessionSeen = Readonly<Record<string, number>>;

/**
 * New output since the viewer last looked: `finished` once the session stopped working
 * (idle or ended), else `unread`. A session never seen has no baseline and shows nothing:
 * `markSessionsSeen` sets one the first time the list shows it. An archived session never shows one.
 */
export function sessionUnread(session: LocalSession, seen: SessionSeen, now = Date.now()): "unread" | "finished" | null {
  const last = seen[session.sessionId];
  if (last === undefined || sessionArchived(session, now) || (session.latestEventSeq ?? 0) <= last) return null;
  return session.status === "idle" || session.status === "ended" ? "finished" : "unread";
}

/** Baselines sessions seen for the first time and advances the open one. Returns `seen` itself when nothing moved. */
export function markSessionsSeen(seen: SessionSeen, sessions: readonly LocalSession[], openSessionId: string | null): SessionSeen {
  let next: Record<string, number> | null = null;
  for (const s of sessions) {
    const seq = s.latestEventSeq ?? 0;
    const last = seen[s.sessionId];
    if (last === undefined || (s.sessionId === openSessionId && seq > last)) {
      next ??= { ...seen };
      next[s.sessionId] = seq;
    }
  }
  return next ?? seen;
}

/** The sidebar/mobile filter: title, folder, tool or owner, case-insensitive. */
export function sessionMatches(session: LocalSession, query: string, ownerName = ""): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  return [sessionTitle(session), session.cwdName ?? "", sessionToolName(session.provider), ownerName]
    .some((field) => field.toLocaleLowerCase().includes(needle));
}
