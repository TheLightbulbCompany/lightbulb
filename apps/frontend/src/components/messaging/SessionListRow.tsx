"use client";

import { useState, useSyncExternalStore } from "react";
import { Archive, ArchiveRestore, MailOpen, MoreHorizontal, Pin, PinOff } from "lucide-react";
import { endedWhy, type LocalSession, sessionAgentName, sessionEndable, sessionSignalsLine, sessionStateLabel, sessionToolName, sessionWaitingLine } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { SessionToolIcon } from "./SessionToolIcon";
import { SessionRenameButton } from "./SessionRenameButton";
import { SessionRemoveButton } from "./SessionRemoveButton";
import { ParticipantAvatar } from "./ParticipantAvatar";
import type { SidebarRow } from "./roomSections";
import { sessionRowAt, sessionRowFailed, sessionRowGone, sessionRowNeedsYou, sessionRowShareable } from "./sessionGroups";

export interface SessionRowMarks {
  pinned: boolean;
  archived: boolean;
  onTogglePin: () => void;
  onToggleArchive: () => void;
  onMarkUnread: (unread: boolean) => void;
}

/** Happy's list clock, by the viewer's calendar: a time today, a weekday within the week, then a date. */
export function sessionListTime(iso: string | null | undefined, now = Date.now()): string {
  const at = Date.parse(iso ?? "");
  if (!at) return "";
  const date = new Date(at);
  const midnight = new Date(now);
  midnight.setHours(0, 0, 0, 0);
  if (at >= midnight.getTime()) return date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  midnight.setDate(midnight.getDate() - 6);
  if (at >= midnight.getTime()) return date.toLocaleDateString([], { weekday: "short" });
  const thisYear = date.getFullYear() === new Date(now).getFullYear();
  return date.toLocaleDateString([], thisYear ? { month: "numeric", day: "numeric" } : { month: "numeric", day: "numeric", year: "2-digit" });
}

const noSubscribe = () => () => {};

/** The viewer's own clock and locale, so it waits for the browser: the server's would not match. */
function SessionListTime({ at }: { at: string | null | undefined }) {
  const browser = useSyncExternalStore(noSubscribe, () => true, () => false);
  return <span className="session-row-time" aria-hidden="true">{browser ? sessionListTime(at) : ""}</span>;
}

/** How long ago, in one unit: "now", "5m", "2h", "3d". Empty without a time. */
export function sessionAge(at: number | undefined, now = Date.now()): string {
  if (!at) return "";
  const minutes = Math.max(0, Math.floor((now - at) / 60_000));
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

const STATE_LABELS = { busy: "Working", waiting: "Waiting on you", idle: "Idle", done: "Done", ended: "Ended" } as const;

/**
 * One session row: the sidebar's list and the All sessions view share it (spec 2026-10-30
 * sessions-sidebar, "States on the row"). Words only where no shape says it: busy is a pulsing dot,
 * waiting is a brass dot, unread a blue or green one; done, offline, parked, ended and failed say
 * so at the right edge. The second line is news only in the sidebar (`oneLine`); All sessions
 * carries the whole description. A row with no backend is not a button: the network knows it, this
 * browser cannot open it.
 */
export function SessionListRow({ entry: { row, owner, unread }, current, onSelect, onRename, onRemove, onShare, oneLine = false, marks }: {
  entry: SidebarRow; current: boolean; onSelect?: (sessionId: string) => void;
  oneLine?: boolean;
  onRename?: (sessionId: string, title: string | null) => Promise<unknown>;
  /** The owner takes a session of their own off the list; a running one is ended first. */
  onRemove?: (sessionId: string) => Promise<unknown>;
  /** Share an unshared terminal session of this Mac's own (rows with no backend). A rejection shows in the row. */
  onShare?: () => Promise<unknown>;
  /** The sidebar's per-viewer row actions (Pin, Mark unread/read, Archive), kept on this browser. */
  marks?: SessionRowMarks;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [refusal, setRefusal] = useState<string>();
  const { backend, state, person } = row;
  const label = `${sessionToolName(row.agent)} · ${row.title}`;
  const gone = sessionRowGone(row);
  const failed = sessionRowFailed(row);
  const why = endedWhy({ status: state === "ended" ? "ended" : "idle", endedReason: row.endedReason as LocalSession["endedReason"] }, person.isViewer ? "your Mac" : `${person.name}'s Mac`);
  const who = person.isViewer ? "your session" : person.name;
  const news = unread === "finished" ? ", finished since you looked" : unread ? ", new output" : "";
  const chatUnread = backend?.chatUnread ?? 0;
  const chat = chatUnread ? `, ${chatUnread} unread in chat` : "";
  const waiting = state === "waiting";
  const waitText = row.waitingFor ?? "Waiting for input";
  const where = why ?? (row.folder ? `in ${row.folder}` : null);
  // Running subagents and the branch's PR (spec 2026-10-26 (f) gaps 16, 15): live rows only.
  const signals = backend && !gone ? sessionSignalsLine(backend) : null;
  const seen = sessionAge(row.lastSeenAt);
  const ended = sessionAge(row.endedAt);
  // A row an engine record joined says its state from `row.state`; the web, with none, keeps its wording.
  const stateText = !row.engine && backend ? sessionStateLabel(backend)
    : state === "offline" ? `Offline${seen ? `, last seen ${seen} ago` : ""}`
      : state === "parked" ? "Parked, wakes when messaged"
        : failed ? "Failed" : STATE_LABELS[state as keyof typeof STATE_LABELS];
  // "On you" is true of the viewer's own session only.
  const stateSaid = waiting && !person.isViewer ? stateText.replace("Waiting on you", "Waiting") : stateText;
  const stateExtra = !row.engine && backend ? sessionWaitingLine(backend) : waiting ? row.waitingFor : null;
  // The word at the right edge, where no shape says the state.
  const word = failed ? `Failed${ended ? ` ${ended}` : ""}`
    : !oneLine && state === "idle" && !row.engine && backend ? stateText
    : !oneLine && state in STATE_LABELS ? (waiting && !person.isViewer ? "Waiting" : STATE_LABELS[state as keyof typeof STATE_LABELS])
    : state === "done" ? "Done"
      : state === "offline" ? `Offline${seen ? ` ${seen}` : ""}`
        : state === "parked" ? "Parked"
          : state === "ended" ? `Ended${ended ? ` ${ended}` : ""}` : "";
  const dot = waiting ? "waiting" : failed ? null : unread ?? null;
  const at = sessionRowAt(row);
  // What needs you is never dimmed: not because it ended, not because it is unshared.
  const needsYou = sessionRowNeedsYou(row);
  const repo = row.repo ? `${row.repo}${row.branch ? ` (${row.branch})` : ""}` : null;
  const resumeHint = row.canResume && !backend ? "resume with lightbulb who" : null;
  const second = oneLine
    ? (waiting ? waitText : failed ? why ?? "Job failed" : !gone && signals ? [row.folder, signals].filter(Boolean).join(" · ") : "")
    : [person.isViewer ? null : person.name, row.computer, repo, row.folder, why ?? (waiting ? waitText : failed ? "Job failed" : null), signals, resumeHint].filter(Boolean).join(" · ");
  const hover = [person.name, sessionAgentName(row.agent), row.computer, repo, row.folder,
    `${stateSaid}${why ? `: ${why}` : ""}`, row.canResume && !backend ? "Resume with lightbulb who" : null].filter(Boolean).join("\n");
  const description = `${label}, ${who}${marks?.pinned ? ", pinned" : ""}${where ? `, ${where}` : ""}, ${stateSaid}${stateExtra && stateExtra !== stateText ? `, ${stateExtra}` : ""}${signals ? `, ${signals}` : ""}${news}${chat}`;
  const face = <span className="session-owner-face">
    <ParticipantAvatar participant={owner} size="sm" showPresence={false} />
    <span className="session-tool-badge session-tool-badge--corner" aria-hidden="true"><SessionToolIcon provider={row.agent} /></span>
  </span>;
  const end = <span className="session-row-end">
    {!!chatUnread && <span className="session-chat-unread" aria-hidden="true">{chatUnread > 99 ? "99+" : chatUnread}</span>}
    {oneLine && state === "busy" && <span className="session-working" aria-hidden="true" />}
    {dot && <span className={`session-unread session-unread--${dot}`} aria-hidden="true" />}
    {word && (dot !== "waiting" || !oneLine) && <span className={`session-row-state${failed ? " session-row-state--failed" : ""}`} aria-hidden="true">{word}</span>}
    {(!oneLine || (!dot && !word)) && <SessionListTime at={at ? new Date(at).toISOString() : null} />}
  </span>;
  const body = <>
    {face}
    <span className="session-row-main" title={hover}>
      <span className="session-row-line">
        <span className={`min-w-0 flex-1 truncate${unread ? " font-semibold" : ""}`}>{row.title}</span>
        {end}
      </span>
      {second && <span className="block truncate text-caption text-muted-foreground">{second}</span>}
      {refusal && <span role="alert" className="block text-caption text-destructive">{refusal}</span>}
    </span>
  </>;
  return <li className={`room-directory-row room-directory-row--roster session-row${gone && !needsYou ? " session-row--gone" : ""}${backend ? "" : " session-row--network"}${needsYou ? " session-row--needs" : ""}`}
    onContextMenu={marks ? (event) => { event.preventDefault(); setMenuOpen(true); } : undefined}>
    {backend
      ? <button type="button" className="room-directory-entry" aria-current={current ? "page" : undefined}
        aria-label={`Open ${description}`} onClick={() => onSelect?.(backend.sessionId)}>{body}</button>
      : <div className="room-directory-entry session-row-info" aria-current={current ? "page" : undefined}>
        <span className="sr-only">{description}</span><span className="contents" aria-hidden="true">{body}</span>
      </div>}
    {onShare && sessionRowShareable(row) && <Button variant="ghost" size="sm" className="session-row-share" aria-label={`Share ${row.title}`} onClick={() => {
      setRefusal(undefined);
      void onShare().catch((error: unknown) => {
        const text = String(error instanceof Error ? error.message : error);
        // The person pressed No: nothing to say.
        if (!/declined on this Mac/i.test(text)) setRefusal(text);
      });
    }}>Share</Button>}
    {marks && <DropdownMenu open={menuOpen} onOpenChange={setMenuOpen}>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" className="room-directory-actions" aria-label={`Session actions for ${row.title}`}><MoreHorizontal aria-hidden="true" /></Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {marks.archived
          ? <DropdownMenuItem onSelect={marks.onToggleArchive}><ArchiveRestore aria-hidden="true" />Unarchive</DropdownMenuItem>
          : <>
            <DropdownMenuItem onSelect={marks.onTogglePin}>{marks.pinned ? <PinOff aria-hidden="true" /> : <Pin aria-hidden="true" />}{marks.pinned ? "Unpin" : "Pin"}</DropdownMenuItem>
            {/* The open session reads itself at once, so Mark unread is offered on the others only; a row with no backend has no read mark. */}
            {backend && (unread || !current) && <DropdownMenuItem onSelect={() => marks.onMarkUnread(!unread)}><MailOpen aria-hidden="true" />{unread ? "Mark read" : "Mark unread"}</DropdownMenuItem>}
            {!(waiting && person.isViewer) && <DropdownMenuItem onSelect={marks.onToggleArchive}><Archive aria-hidden="true" />Archive</DropdownMenuItem>}
          </>}
      </DropdownMenuContent>
    </DropdownMenu>}
    {!oneLine && backend && person.isViewer && (onRename || onRemove) && <span className="session-row-tools">
      {onRename && <SessionRenameButton session={backend} onRename={onRename} icon />}
      {onRemove && (backend.status === "ended" || sessionEndable(backend))
        && <SessionRemoveButton name={row.title} live={backend.status !== "ended"} onRemove={() => onRemove(backend.sessionId)} icon />}
    </span>}
  </li>;
}
