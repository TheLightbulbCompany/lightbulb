import type { ChatEntry, LocalSession, RoomMember, RoomSummary } from "@lightbulb/shared";

import { LocalStore } from "./localStore";

/**
 * The Chats tab's per-device state: sessions archived from the list, one map
 * per workspace, stored on the phone only.
 */
const store = new LocalStore<Record<string, unknown>>("chats");

export const archivedSessionsKey = (orgId: string) => `lightbulb.archivedSessions.${orgId}`;

function put(key: string, value: unknown): void {
  store.write({ ...store.read(), [key]: value });
}

export function readArchivedSessions(orgId: string): Record<string, number> {
  const raw = store.read()[archivedSessionsKey(orgId)];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
  return Object.fromEntries(Object.entries(raw).filter(([, seq]) => typeof seq === "number")) as Record<string, number>;
}

/** Hidden from Chats until the session has output past this seq (see `mergeChats`). */
export function archiveSession(orgId: string, session: Pick<LocalSession, "sessionId" | "latestEventSeq">): void {
  put(archivedSessionsKey(orgId), { ...readArchivedSessions(orgId), [session.sessionId]: session.latestEventSeq ?? 0 });
}

/**
 * What a row needs of a member: the name it shows, the tile it draws, and
 * whether it's currently live. `online` comes from `usePresence` (Task P1;
 * humans only) and `sharing` from whether the member's session is live.
 */
export type RowParticipant = Pick<RoomMember, "kind" | "displayName" | "id"> & {
  online?: boolean;
  sharing?: boolean;
};

/** The room's members minus the viewer — a DM is named after the other party. */
export const otherMembers = (
  room: Pick<RoomSummary, "members"> | undefined,
  viewerId: string | null | undefined,
): RowParticipant[] => (room?.members ?? []).filter((m) => !(m.kind === "human" && m.id === viewerId));

/** A chat row, or the super-agent's Specialists group row. */
export type ChatListRow = ChatEntry | { kind: "specialists"; key: string; count: number; open: boolean };

/** Super-agent one voice: DMs with any agent but the front one (`@lightbulb`) move under one Specialists row at
 *  the end, listed only while `open`. No front agent known: the entries as they are. */
export function fileSpecialists(entries: ChatEntry[], frontAgentId: string | undefined, viewerId: string | null | undefined, open: boolean): ChatListRow[] {
  if (!frontAgentId) return entries;
  const specialist = (entry: ChatEntry) => {
    if (entry.kind !== "room" || entry.room.kind !== "dm") return false;
    const others = otherMembers(entry.room, viewerId);
    return others.length === 1 && others[0].kind === "agent" && others[0].id !== frontAgentId;
  };
  const grouped = entries.filter(specialist);
  if (!grouped.length) return entries;
  return [...entries.filter((e) => !specialist(e)), { kind: "specialists", key: "specialists", count: grouped.length, open }, ...(open ? grouped : [])];
}
