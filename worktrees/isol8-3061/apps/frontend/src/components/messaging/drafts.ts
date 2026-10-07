import type { MemberRef } from "@lightbulb/shared";

const PREFIX = "lightbulb.messaging.draft|";

interface StoredDraft {
  text: string;
  mentions: MemberRef[];
}

const EMPTY: StoredDraft = { text: "", mentions: [] };

/** `lightbulb.messaging.draft|<owner>|<room>|<thread or empty>` — `|` appears in no Clerk or room id. */
export function draftKey(ownerScopeId: string, roomId: string, threadId: string | null): string {
  return `${PREFIX}${ownerScopeId}|${roomId}|${threadId ?? ""}`;
}

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
    };
  } catch {
    return { ...EMPTY };
  }
}

export function writeDraft(key: string, draft: StoredDraft): void {
  try {
    if (!draft.text.trim() && draft.mentions.length === 0) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(draft));
  } catch {
    /* per-viewer convenience only */
  }
}

export function clearDraft(key: string): void {
  try {
    localStorage.removeItem(key);
  } catch {
    /* per-viewer convenience only */
  }
}

/** Room ids with a non-empty draft for this owner, in insertion order, de-duplicated. */
export function listDraftRoomIds(ownerScopeId: string): string[] {
  const head = `${PREFIX}${ownerScopeId}|`;
  const rooms: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(head)) continue;
      const roomId = key.slice(head.length).split("|")[0];
      const draft = readDraft(key);
      if (!roomId || (!draft.text.trim() && draft.mentions.length === 0)) continue;
      if (!rooms.includes(roomId)) rooms.push(roomId);
    }
  } catch {
    return [];
  }
  return rooms;
}
