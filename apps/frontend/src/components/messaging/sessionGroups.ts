import { sessionArchived, sessionArchivedByViewer, sessionToolName, type SessionMarks, type SessionRow } from "@lightbulb/shared";
import type { SidebarRow } from "./roomSections";

export type SessionGroupBy = "person" | "state" | "repo";
export type SessionShow = "all" | "mine" | "live";
/** Rows a group shows before "… N more" (pinned and open ones stay past it). */
export const SESSION_GROUP_FOLD = 5;
export const NEEDS_YOU_GROUP = "needs-you";
export const NETWORK_GROUP = "network";
export interface SessionGroup { id: string; label: string; entries: SidebarRow[] }

/** The key marks (pins, archives) and the open session go by: the backend id when there is one. */
export const sessionRowKey = (row: SessionRow): string => row.backend?.sessionId ?? row.id;

/** Ended badly: its job failed, or its Mac's login session restarted under it. */
export const sessionRowFailed = (row: SessionRow): boolean =>
  row.state === "ended" && (row.engine?.job?.state === "failed" || row.endedReason === "login-session-ended");

export const sessionRowNeedsYou = (row: SessionRow): boolean => row.person.isViewer && (row.state === "waiting" || sessionRowFailed(row));
export const sessionRowGone = (row: SessionRow): boolean => row.state === "offline" || row.state === "ended";

export function sessionRowAt(row: SessionRow): number {
  const iso = row.backend?.lastEventAt ?? row.backend?.endedAt;
  return (iso ? Date.parse(iso) : NaN) || (row.endedAt ?? row.lastSeenAt ?? (row.engine?.seen_at ?? 0) * 1000);
}

/** `own`, not `isViewer`: the viewer's other Mac is theirs too, and this Mac cannot share its sessions. */
export const sessionRowShareable = (row: SessionRow): boolean =>
  !row.backend && row.engine?.own === true && row.agent === "claude" && !["offline", "parked", "ended"].includes(row.state);

const STATES: [SessionRow["state"], string, string][] = [
  ["waiting", "Waiting", "waiting"], ["busy", "Working", "working"], ["idle", "Idle", "idle"], ["done", "Done", "done"],
  ["parked", "Parked", "parked"], ["offline", "Offline", "offline"], ["ended", "Ended", "ended"],
];

export function sessionRowMatches(row: SessionRow, query: string): boolean {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return true;
  const word = STATES.find(([state]) => state === row.state)?.[2];
  const pr = row.backend?.pr;
  return [row.title, row.folder, row.repo, row.branch, sessionToolName(row.agent), row.agent, row.person.name, row.person.isViewer ? "you" : undefined, row.computer,
    word, sessionRowFailed(row) ? "failed" : undefined, row.waitingFor, pr ? `#${pr.number}` : undefined]
    .some((field) => field?.toLocaleLowerCase().includes(needle));
}

export const ownWaitingCount = (rows: readonly SessionRow[]): number => rows.filter((row) => row.person.isViewer && row.state === "waiting").length;

/** Offline for days with no engine record: never listed (an engine record means the network still knows it). */
export const sessionRowDropped = (row: SessionRow, now = Date.now()): boolean => !row.engine && !!row.backend && sessionArchived(row.backend, now);

export function sessionRowGroups(
  entries: readonly SidebarRow[],
  { groupBy, show, query, marks, now = Date.now() }: { groupBy: SessionGroupBy; show: SessionShow; query: string; marks: SessionMarks; now?: number },
): { groups: SessionGroup[]; archived: SidebarRow[]; hidden: number; listed: number } {
  const kept = entries.filter((entry) => !sessionRowDropped(entry.row, now));
  const archived: SidebarRow[] = [];
  const shown: SidebarRow[] = [];
  for (const entry of kept) {
    const { row } = entry;
    const away = !(row.person.isViewer && row.state === "waiting")
      && sessionArchivedByViewer({ sessionId: sessionRowKey(row), latestEventSeq: row.backend?.latestEventSeq ?? 0 }, marks.archived);
    (away ? archived : shown).push(entry);
  }
  let hidden = 0;
  const needs: SidebarRow[] = [];
  const network: SidebarRow[] = [];
  const connections: SidebarRow[] = [];
  const rest = new Map<string, SessionGroup>();
  for (const entry of shown) {
    const { row } = entry;
    if (!sessionRowMatches(row, query)) continue;
    const needsYou = sessionRowNeedsYou(row);
    if (!needsYou && (show === "mine" ? !row.person.isViewer : show === "live" && sessionRowGone(row))) { hidden++; continue; }
    // Hosted MCP clients are messaging connections, not terminals to open. Claude
    // shares its agent name with the CLI; its declared app record has no folder.
    const assistant = row.agent === "chatgpt" || (row.agent === "claude" && row.engine?.tier === "pull" && row.engine.folder === "" && !row.engine.repo);
    if (row.source === "network" && assistant) connections.push(entry);
    else if (needsYou) needs.push(entry);
    else if (row.source === "network" && !row.person.isViewer && !row.person.member) network.push(entry);
    else {
      const [id, label] = groupBy === "state" ? [`state:${row.state}`, STATES.find(([state]) => state === row.state)![1]]
        : groupBy === "repo" ? [`repo:${row.repo ?? row.folder ?? ""}`, row.repo ?? row.folder ?? "No repo"]
          : [`person:${row.person.userId ?? row.person.name}`, row.person.isViewer ? "You" : row.person.name];
      if (!rest.has(id)) rest.set(id, { id, label, entries: [] });
      rest.get(id)!.entries.push(entry);
    }
  }
  const viewerFirst = (group: SessionGroup) => Number(!group.entries[0].row.person.isViewer);
  const ordered = [...rest.values()];
  if (groupBy === "state") ordered.sort((a, b) => STATES.findIndex(([s]) => `state:${s}` === a.id) - STATES.findIndex(([s]) => `state:${s}` === b.id));
  else if (groupBy === "repo") ordered.sort((a, b) => Number(a.id === "repo:") - Number(b.id === "repo:") || a.label.localeCompare(b.label));
  else ordered.sort((a, b) => viewerFirst(a) - viewerFirst(b) || a.label.localeCompare(b.label));
  const pinned = new Set(marks.pinned);
  const sort = (list: SidebarRow[]) => list.sort((a, b) => {
    const rank = ({ row }: SidebarRow) => (pinned.has(sessionRowKey(row)) ? 0 : 2) + Number(sessionRowGone(row));
    return rank(a) - rank(b) || sessionRowAt(b.row) - sessionRowAt(a.row) || sessionRowKey(a.row).localeCompare(sessionRowKey(b.row));
  });
  const groups = [
    { id: NEEDS_YOU_GROUP, label: "Needs you", entries: needs }, ...ordered, { id: NETWORK_GROUP, label: "On the network", entries: network },
    { id: "connections", label: "Connected assistants", entries: connections },
  ].filter((group) => group.entries.length > 0);
  for (const group of groups) sort(group.entries);
  return { groups, archived, hidden, listed: kept.length };
}

/** The first `limit` entries plus every later one `keep` names (pinned, open); `limit: 0` is the collapsed case. */
export const foldedEntries = (entries: readonly SidebarRow[], { limit, keep }: { limit: number; keep: (entry: SidebarRow) => boolean }): SidebarRow[] =>
  entries.filter((entry, index) => index < limit || keep(entry));
