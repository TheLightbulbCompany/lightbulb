import type { MemberRef } from "@lightbulb/shared";

const PREFIX = "lightbulb.messaging.draft|";

export interface StoredDraft {
  text: string;
  mentions: MemberRef[];
  /** When the person last edited it (ms). A cleared draft (sent, emptied) is kept as an empty row with its time, so a
   *  second tab still holding the old text can never write it back over the clear (W17-7). */
  at: number;
}

const EMPTY: StoredDraft = { text: "", mentions: [], at: 0 };
/** Cleared rows older than this are dropped when the list is read. ponytail: a day; a stale tab older than that wins. */
const TOMBSTONE_MS = 24 * 60 * 60 * 1000;

/** `lightbulb.messaging.draft|<owner>|<room>|<thread or empty>` — `|` appears in no Clerk or room id. */
export function draftKey(ownerScopeId: string, roomId: string, threadId: string | null): string {
  return `${PREFIX}${ownerScopeId}|${roomId}|${threadId ?? ""}`;
}

export const isDraftKey = (key: string | null): key is string => !!key?.startsWith(PREFIX);

export function readDraft(key: string): StoredDraft {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return { ...EMPTY };
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return { ...EMPTY };
    const record = parsed as Partial<StoredDraft>;
    return {
      text: typeof record.text === "string" ? record.text : "",
      mentions: Array.isArray(record.mentions)
        ? record.mentions.filter((ref): ref is MemberRef =>
            !!ref && (ref.kind === "human" || ref.kind === "agent") && typeof ref.id === "string")
        : [],
      at: typeof record.at === "number" ? record.at : 0,
    };
  } catch {
    return { ...EMPTY };
  }
}

/** Writes the draft as edited at `at`, unless the stored one is newer (another tab, or a send). False when skipped. */
export function writeDraft(key: string, draft: { text: string; mentions: MemberRef[] }, at = Date.now()): boolean {
  try {
    if (readDraft(key).at > at) return false;
    const empty = !draft.text.trim() && draft.mentions.length === 0;
    localStorage.setItem(key, JSON.stringify(empty ? { text: "", mentions: [], at } : { ...draft, at }));
    return true;
  } catch {
    return false; /* per-viewer convenience only */
  }
}

/** A send or an emptied box: the newest state of this draft, now. */
export function clearDraft(key: string): void {
  writeDraft(key, { text: "", mentions: [] });
}

/** Room ids with a non-empty draft for this owner, in insertion order, de-duplicated. */
export function listDraftRoomIds(ownerScopeId: string): string[] {
  const head = `${PREFIX}${ownerScopeId}|`;
  const rooms: string[] = [];
  const expired: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(head)) continue;
      const roomId = key.slice(head.length).split("|")[0];
      const draft = readDraft(key);
      if (!draft.text.trim() && draft.mentions.length === 0) {
        if (Date.now() - draft.at > TOMBSTONE_MS) expired.push(key);
        continue;
      }
      if (roomId && !rooms.includes(roomId)) rooms.push(roomId);
    }
    for (const key of expired) localStorage.removeItem(key);
  } catch {
    return [];
  }
  return rooms;
}
