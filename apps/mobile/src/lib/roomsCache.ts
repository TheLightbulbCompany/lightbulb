import {
  ROOMS_SNAPSHOT_VERSION,
  type RoomsPersistence,
  type RoomsSnapshot,
} from "@lightbulb/shared";
import { Directory, File, Paths } from "expo-file-system";
import { AppState } from "react-native";

// ---------------------------------------------------------------------------
// The room list and recent history, kept across launches so a cold start paints
// the last-known conversations instantly and the network reconciles behind it
// (useRooms' `persistence` seam; the web passes none).
//
// One JSON file per `userId:orgId` scope under the DOCUMENT directory — see
// localStore.ts for why expo-file-system (already in the binary, OTA-safe) and
// why not the evictable cache directory. Files get iOS's default data
// protection (encrypted at rest until first unlock), same as the drafts store.
//
// Security rules: a file is read only for the exact scope that wrote it, a
// scope that is not a plain id pair is never touched, a file of another
// version or a corrupt one is deleted rather than read, and signing out
// (clearRoomsCache, from the (app) layout) deletes every file.
// ---------------------------------------------------------------------------

/** Writes land at most this long after the first change; backgrounding flushes at once. */
const WRITE_DELAY_MS = 2_000;
const DIR = "rooms-cache";
const SCOPE = /^([A-Za-z0-9_-]+):([A-Za-z0-9_-]+)$/;

const fileFor = (scope: string): File | null => {
  const match = SCOPE.exec(scope);
  return match ? new File(Paths.document, DIR, `${match[1]}__${match[2]}.json`) : null;
};

const pending = new Map<string, RoomsSnapshot>();
let timer: ReturnType<typeof setTimeout> | null = null;
let listening = false;

function flush(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  for (const [scope, snapshot] of pending) {
    try {
      const directory = new Directory(Paths.document, DIR);
      if (!directory.exists) directory.create({ intermediates: true });
      const file = fileFor(scope)!;
      if (!file.exists) file.create();
      file.write(JSON.stringify({ version: ROOMS_SNAPSHOT_VERSION, ...snapshot }));
    } catch {
      /* a cache: the next launch simply waits for the network */
    }
  }
  pending.clear();
}

export const roomsCache: RoomsPersistence = {
  load(scope) {
    const file = fileFor(scope);
    if (!file?.exists) return null;
    try {
      const parsed = JSON.parse(file.textSync()) as Partial<RoomsSnapshot> & { version?: unknown };
      if (
        parsed?.version === ROOMS_SNAPSHOT_VERSION &&
        Array.isArray(parsed.rooms?.rooms) &&
        Array.isArray(parsed.rooms?.sections) &&
        Array.isArray(parsed.history)
      ) {
        return { rooms: parsed.rooms, history: parsed.history };
      }
    } catch {
      /* corrupt: discarded below */
    }
    try {
      file.delete();
    } catch {
      /* the next save overwrites it anyway */
    }
    return null;
  },
  save(scope, snapshot) {
    if (!fileFor(scope)) return;
    pending.set(scope, snapshot);
    if (!listening) {
      listening = true;
      AppState.addEventListener("change", (state) => {
        if (state !== "active") flush();
      });
    }
    timer ??= setTimeout(flush, WRITE_DELAY_MS);
  },
};

/** Sign-out: drop any pending write and delete every scope's file. */
export function clearRoomsCache(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  pending.clear();
  try {
    const directory = new Directory(Paths.document, DIR);
    if (directory.exists) directory.delete();
  } catch {
    /* nothing to delete */
  }
}
