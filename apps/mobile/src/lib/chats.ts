import {
  agentRingState,
  roomDisplayName,
  sessionToolName,
  type AvatarRing,
  type LocalSession,
  type RoomMember,
  type RoomSummary,
} from "@lightbulb/shared";

import { LocalStore } from "./localStore";

/**
 * The Chats tab's per-device state — pins, sessions archived from the list and
 * the "Show running sessions in pinned" switch — plus the pure resolution of
 * pins into what the strip draws. Pins are stored on the phone only (owner
 * ruling 2026-09-26: no server-side sync), ids only, one list per workspace.
 */
const store = new LocalStore<Record<string, unknown>>("chats");

export type PinKey = `agent:${string}` | `room:${string}`;
const PIN = /^(agent|room):.+$/;

export const pinsKey = (orgId: string) => `lightbulb.pins.${orgId}`;
export const archivedSessionsKey = (orgId: string) => `lightbulb.archivedSessions.${orgId}`;
export const RUNNING_SESSIONS_KEY = "lightbulb.pinRunningSessions";

function put(key: string, value: unknown): void {
  store.write({ ...store.read(), [key]: value });
}

/** A stored value that parses as a pin list, else null (malformed == never edited). */
export function parsePins(raw: unknown): PinKey[] | null {
  if (!Array.isArray(raw) || !raw.every((key) => typeof key === "string" && PIN.test(key))) return null;
  return [...new Set(raw as PinKey[])];
}

/** This workspace's pins, or null when the reader never edited them (the strip shows the defaults). */
export function readPins(orgId: string): PinKey[] | null {
  return parsePins(store.read()[pinsKey(orgId)]);
}

export function writePins(orgId: string, pins: readonly PinKey[]): void {
  put(pinsKey(orgId), [...new Set(pins)]);
}

/** Stored pins, else the workspace's starter agents in roster order. Never writes. */
export const resolvePins = (stored: readonly PinKey[] | null, seededAgentIds: readonly string[]): PinKey[] =>
  stored ? [...stored] : seededAgentIds.map((id): PinKey => `agent:${id}`);

export const togglePin = (pins: readonly PinKey[], key: PinKey): PinKey[] =>
  pins.includes(key) ? pins.filter((pin) => pin !== key) : [...pins, key];

export function movePin(pins: readonly PinKey[], key: PinKey, delta: -1 | 1): PinKey[] {
  const from = pins.indexOf(key);
  const to = from + delta;
  const next = [...pins];
  if (from < 0 || to < 0 || to >= pins.length) return next;
  [next[from], next[to]] = [next[to], next[from]];
  return next;
}

/** Default on (spec): only an explicit false turns it off. */
export const readShowRunningSessions = (): boolean => store.read()[RUNNING_SESSIONS_KEY] !== false;
export const writeShowRunningSessions = (on: boolean): void => put(RUNNING_SESSIONS_KEY, on);

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

/** A 1:1 DM with an agent pins the AGENT (it survives the DM being recreated); anything else pins the room. */
export function pinKeyForRoom(
  room: Pick<RoomSummary, "roomId" | "kind">,
  others: readonly Pick<RoomMember, "kind" | "id">[],
): PinKey {
  return room.kind === "dm" && others.length === 1 && others[0].kind === "agent"
    ? `agent:${others[0].id}`
    : `room:${room.roomId}`;
}

export interface RosterAgent {
  id: string;
  name?: string | null;
  status?: string | null;
  seeded?: boolean;
}

export type PinnedItem =
  | { key: PinKey; kind: "agent"; agentId: string; name: string; ring: AvatarRing | null; roomId: string | null }
  | { key: PinKey; kind: "room"; room: RoomSummary; name: string; others: RowParticipant[] }
  | { key: `session:${string}`; kind: "session"; session: LocalSession; name: string };

/**
 * What the strip draws: busy sessions first (when the switch is on, newest
 * first), then the pins in order. A pin whose target is gone (agent removed,
 * room left) is skipped, not deleted — the stored list only changes on the
 * reader's next edit.
 */
export function buildPinnedItems({
  pins, rooms, agents, sessions, showRunningSessions, viewerId,
}: {
  pins: readonly PinKey[];
  rooms: readonly RoomSummary[];
  agents: readonly RosterAgent[];
  sessions: readonly LocalSession[];
  showRunningSessions: boolean;
  viewerId: string | null | undefined;
}): PinnedItem[] {
  const running: PinnedItem[] = showRunningSessions
    ? sessions
        .filter((session) => session.status === "busy")
        .sort((a, b) => (b.lastEventAt ?? "").localeCompare(a.lastEventAt ?? ""))
        .map((session) => ({
          key: `session:${session.sessionId}` as const,
          kind: "session" as const,
          session,
          name: sessionToolName(session.provider),
        }))
    : [];
  const pinned = pins.flatMap((key): PinnedItem[] => {
    const id = key.slice(key.indexOf(":") + 1);
    if (key.startsWith("room:")) {
      const room = rooms.find((candidate) => candidate.roomId === id);
      if (!room) return [];
      const others = otherMembers(room, viewerId);
      return [{ key, kind: "room", room, name: roomDisplayName(room, others), others }];
    }
    const dm = rooms.find(
      (candidate) => candidate.kind === "dm" && otherMembers(candidate, viewerId).some((m) => m.kind === "agent" && m.id === id),
    );
    const agent = agents.find((candidate) => candidate.id === id);
    const name = agent?.name || dm?.members.find((m) => m.kind === "agent" && m.id === id)?.displayName;
    if (!name) return [];
    return [{ key, kind: "agent", agentId: id, name, ring: agentRingState(agent?.status), roomId: dm?.roomId ?? null }];
  });
  return [...running, ...pinned];
}
