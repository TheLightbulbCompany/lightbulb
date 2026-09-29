import { useAuth } from "@clerk/clerk-expo";
import {
  ApiError,
  MESSAGE_ATTACHMENT_MAX_BYTES,
  type MessageAttachment,
  type RoomAttachmentInput,
} from "@lightbulb/shared";
import { useCallback, useEffect, useRef, useState } from "react";

import { uploadPart } from "./attachmentFile";
import { LocalStore } from "./localStore";

// ---------------------------------------------------------------------------
// Composer drafts — what you typed, and what you attached, kept when you leave.
//
// Backing out of a room used to throw the message away, because the text lived
// in the composer's own `useState`. Slack keeps it, and so does the web app
// (`components/messaging/drafts.ts`, in localStorage); this is that, on the
// device store.
//
// A photo is part of the draft too, which is why it has to upload when it is
// PICKED rather than when the message is sent: the draft can then hold the
// attachment's server id, and a restored draft sends without re-uploading
// anything. Uploading on pick also means sending is instant, and one failed
// photo no longer takes the whole message down with it.
// ---------------------------------------------------------------------------

/** The content types the messaging endpoint accepts (see the shared cap map). */
type ContentType = MessageAttachment["contentType"];

export interface StagedAttachment {
  /** Stable key for the chip list; not the server's id. */
  key: string;
  name: string;
  contentType: ContentType;
  /** Local file, for the thumbnail. May be gone after a restart. */
  uri: string;
  size: number;
  /** The server's id, once the upload lands. Null while it is in flight. */
  attachmentId: string | null;
  /** The upload failed and the chip offers a retry. */
  failed: boolean;
}

export interface ComposerDraft {
  text: string;
  attachments: StagedAttachment[];
}

interface StoredDraft {
  text: string;
  /** Only uploaded attachments are stored — an id is what makes one resendable. */
  attachments: StagedAttachment[];
}

const EMPTY: ComposerDraft = { text: "", attachments: [] };

// `|` appears in no Clerk id, room id or message id.
const store = new LocalStore<Record<string, StoredDraft>>("composer-drafts");

/** `<owner>|<room>|<thread or empty>` — the web key scheme, same order. */
export function draftKey(ownerScopeId: string, roomId: string, threadId: string | null): string {
  return `${ownerScopeId}|${roomId}|${threadId ?? ""}`;
}

/**
 * The key for the conversation on screen, scoped to whoever is signed in —
 * the same `user:org` scope the room machine keys its own caches on, so a
 * shared device never shows one person's draft to another.
 */
export function useDraftKey(roomId: string | undefined, threadId: string | null): string | null {
  const { userId, orgId } = useAuth();
  if (!userId || !roomId) return null;
  return draftKey(`${userId}:${orgId ?? userId}`, roomId, threadId);
}

/** Rooms this person has an unsent draft in — the room list's "Draft" tag. */
export function roomIdsWithDrafts(ownerScopeId: string): Set<string> {
  const head = `${ownerScopeId}|`;
  const rooms = new Set<string>();
  for (const [key, draft] of Object.entries(store.read())) {
    if (!key.startsWith(head)) continue;
    if (!draft.text.trim() && draft.attachments.length === 0) continue;
    const roomId = key.slice(head.length).split("|")[0];
    if (roomId) rooms.add(roomId);
  }
  return rooms;
}

/** The draft stored for a conversation, or an empty one. */
export function readDraft(key: string): ComposerDraft {
  const stored = store.read()[key];
  if (!stored) return EMPTY;
  return {
    text: typeof stored.text === "string" ? stored.text : "",
    attachments: Array.isArray(stored.attachments)
      ? stored.attachments.filter((item) => item && typeof item.attachmentId === "string")
      : [],
  };
}

/** Stores a draft, keeping only what would still be sendable later. */
export function saveDraft(key: string, draft: ComposerDraft): void {
  const next = { ...store.read() };
  // Only attachments that finished uploading: an id is what makes one
  // resendable later, and a local uri may not survive the trip anyway.
  const attachments = draft.attachments.filter((item) => item.attachmentId && !item.failed);
  if (!draft.text.trim() && attachments.length === 0) delete next[key];
  else next[key] = { text: draft.text, attachments };
  store.write(next);
}

/** Writes any debounced draft out now — for backgrounding. */
export function flushDrafts(): void {
  store.flush();
}

/**
 * Whether the messaging endpoint will take this photo, and why not if it will
 * not. The cap is the SHARED one (`MESSAGE_ATTACHMENT_MAX_BYTES`, 5 MB for an
 * image): a larger one used to pass here and fail on the server.
 */
export function checkPhoto(
  contentType: string,
  size: number,
): { contentType: ContentType; error: null } | { contentType: null; error: string } {
  const cap = MESSAGE_ATTACHMENT_MAX_BYTES[contentType as ContentType];
  if (cap == null) {
    return { contentType: null, error: "That photo is in a format we can't send. Try a JPEG or PNG." };
  }
  if (size > cap) {
    const limit = Math.round(cap / (1024 * 1024));
    const actual = (size / (1024 * 1024)).toFixed(1);
    return { contentType: null, error: `That photo is ${actual}MB. The limit is ${limit}MB.` };
  }
  return { contentType: contentType as ContentType, error: null };
}

/**
 * The composer's text and attachments, restored from and written back to the
 * device store, with attachments uploading as they are picked.
 *
 * `key` identifies the conversation — a room's own timeline and each of its
 * threads draft separately, exactly as on the web.
 */
export function useComposerDraft({
  key,
  uploadAttachment,
}: {
  key: string | null;
  uploadAttachment: (file: RoomAttachmentInput) => Promise<{ id: string }>;
}) {
  const [draft, setDraft] = useState<ComposerDraft>(() => (key ? readDraft(key) : EMPTY));
  const [uploadError, setUploadError] = useState<string | null>(null);
  // Upload needs the live callback without re-creating `stage` on every render.
  const uploadRef = useRef(uploadAttachment);
  uploadRef.current = uploadAttachment;
  const nextKey = useRef(0);

  // Persisting is an effect, not something done inside the state updater: an
  // updater has to stay pure (React may call it more than once for a single
  // update, and the compiler assumes it is free of side effects).
  useEffect(() => {
    if (key) saveDraft(key, draft);
  }, [key, draft]);

  // Leaving the room is the very case this exists for, and leaving unmounts
  // the composer — so the debounced write is forced out here rather than
  // being raced by the screen going away.
  useEffect(() => () => flushDrafts(), []);

  const update = useCallback(
    (change: (current: ComposerDraft) => ComposerDraft) => setDraft(change),
    [],
  );

  const setText = useCallback(
    (text: string) => update((current) => ({ ...current, text })),
    [update],
  );

  const upload = useCallback(
    async (item: StagedAttachment) => {
      setUploadError(null);
      try {
        const { id } = await uploadRef.current(
          uploadPart({ uri: item.uri, name: item.name, type: item.contentType }),
        );
        update((current) => ({
          ...current,
          attachments: current.attachments.map((entry) =>
            entry.key === item.key ? { ...entry, attachmentId: id, failed: false } : entry,
          ),
        }));
      } catch (error) {
        // The server's own words when it refused the file; a failure that
        // never reached it is the connection's.
        setUploadError(
          error instanceof ApiError
            ? error.message
            : `${item.name} didn't upload. Check your connection, then retry.`,
        );
        update((current) => ({
          ...current,
          attachments: current.attachments.map((entry) =>
            entry.key === item.key ? { ...entry, failed: true } : entry,
          ),
        }));
      }
    },
    [update],
  );

  /** Adds a picked photo and starts its upload straight away. */
  const stage = useCallback(
    (photo: { uri: string; name: string; contentType: ContentType; size: number }) => {
      const item: StagedAttachment = {
        key: `${photo.uri}#${nextKey.current++}`,
        name: photo.name,
        contentType: photo.contentType,
        uri: photo.uri,
        size: photo.size,
        attachmentId: null,
        failed: false,
      };
      update((current) => ({ ...current, attachments: [...current.attachments, item] }));
      void upload(item);
    },
    [update, upload],
  );

  const retry = useCallback(
    (attachmentKey: string) => {
      const item = draft.attachments.find((entry) => entry.key === attachmentKey);
      if (!item || item.attachmentId) return;
      update((current) => ({
        ...current,
        attachments: current.attachments.map((entry) =>
          entry.key === attachmentKey ? { ...entry, failed: false } : entry,
        ),
      }));
      void upload(item);
    },
    [draft.attachments, update, upload],
  );

  const remove = useCallback(
    (attachmentKey: string) => {
      setUploadError(null);
      update((current) => ({
        ...current,
        attachments: current.attachments.filter((entry) => entry.key !== attachmentKey),
      }));
    },
    [update],
  );

  const clear = useCallback(() => update(() => EMPTY), [update]);

  return {
    text: draft.text,
    attachments: draft.attachments,
    setText,
    stage,
    retry,
    remove,
    clear,
    /** At least one upload is still in flight, so Send has to wait. */
    uploading: draft.attachments.some((item) => !item.attachmentId && !item.failed),
    /** Why the last upload failed, until it is retried or removed. */
    uploadError,
    /** At least one upload failed; the reader retries or removes it. */
    failed: draft.attachments.some((item) => item.failed),
    attachmentIds: draft.attachments
      .map((item) => item.attachmentId)
      .filter((id): id is string => !!id),
  };
}
