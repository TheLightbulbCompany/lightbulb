import { ROOMS_SNAPSHOT_VERSION, type RoomsPersistence } from "@lightbulb/shared";

// The last room list this browser saw for one person in one workspace, so a reload draws Home's projects and the
// sidebar at once while the server's list loads behind it (W14-4). useRooms' `persistence` seam, as on mobile, but the
// list only: no message history at rest in the browser. Read only for the exact `userId:orgId` scope that wrote it;
// signing out drops every scope (clearRoomsCache, from SwrProvider). Blocked storage reads as "no snapshot".

const PREFIX = "lightbulb.rooms:";
const WRITE_DELAY_MS = 1_000;
const pending = new Map<string, string>();
let timer: ReturnType<typeof setTimeout> | null = null;

function flush(): void {
  timer = null;
  for (const [key, value] of pending) {
    try { localStorage.setItem(key, value); } catch { /* full or blocked: the next load waits for the network */ }
  }
  pending.clear();
}

export const roomsCache: RoomsPersistence = {
  load(scope) {
    try {
      const parsed = JSON.parse(localStorage.getItem(PREFIX + scope) ?? "null") as
        { version?: unknown; rooms?: { rooms?: unknown; sections?: unknown } } | null;
      if (parsed?.version === ROOMS_SNAPSHOT_VERSION && Array.isArray(parsed.rooms?.rooms) && Array.isArray(parsed.rooms?.sections)) {
        return { rooms: parsed.rooms as never, history: [] };
      }
    } catch { /* corrupt or blocked: no snapshot */ }
    return null;
  },
  save(scope, snapshot) {
    pending.set(PREFIX + scope, JSON.stringify({ version: ROOMS_SNAPSHOT_VERSION, rooms: snapshot.rooms }));
    timer ??= setTimeout(flush, WRITE_DELAY_MS);
  },
};

/** Sign-out: drop any pending write and every scope's list. */
export function clearRoomsCache(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  pending.clear();
  try {
    const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i));
    for (const key of keys) if (key?.startsWith(PREFIX)) localStorage.removeItem(key);
  } catch { /* nothing to drop */ }
}
