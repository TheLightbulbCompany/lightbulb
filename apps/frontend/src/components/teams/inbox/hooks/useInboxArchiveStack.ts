// apps/frontend/src/components/teams/inbox/hooks/useInboxArchiveStack.ts

// Ported from upstream Paperclip's pages/Inbox.tsx archiveIssueMutation +
// unarchiveIssueMutation + archive-stack state (paperclip/ui/src/pages/Inbox.tsx:1407-1582)
// (MIT, (c) 2025 Paperclip AI). Translated from React Query mutations to SWR
// cache mutations via useSWRConfig().mutate.

import { useState, useCallback } from "react";
import { flushSync } from "react-dom";
import { useSWRConfig } from "swr";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import type { Issue } from "@/components/teams/shared/types";

const INBOX_KEYS = [
  teamsQueryKeys.inbox.list("mine", {}),
  teamsQueryKeys.inbox.list("recent", {}),
  teamsQueryKeys.inbox.list("all", {}),
];

// SWR cache keys are stored with a "/teams" prefix (useTeamsApi composes them).
// We use the same composition when calling mutate() so the cache lookup hits.
const SWR_PREFIX = "/teams";
const swrKey = (path: string) => `${SWR_PREFIX}${path}`;

type IssueListData = { items: Issue[] } | Issue[];

function withoutId(
  data: IssueListData | undefined,
  id: string,
): IssueListData | undefined {
  if (!data) return data;
  if (Array.isArray(data)) return data.filter((i) => i.id !== id);
  return { ...data, items: (data.items ?? []).filter((i) => i.id !== id) };
}

function setIssueReadFlag(
  data: IssueListData | undefined,
  issueId: string,
  unread: boolean,
): IssueListData | undefined {
  if (!data) return data;
  if (Array.isArray(data)) {
    return data.map((i) => (i.id === issueId ? { ...i, unread } : i));
  }
  return {
    ...data,
    items: (data.items ?? []).map((i) =>
      i.id === issueId ? { ...i, unread } : i,
    ),
  };
}

export interface UseInboxArchiveStackResult {
  /** Set of issue IDs currently being archived (for fade-out CSS class). */
  archivingIssueIds: Set<string>;
  /** Whether at least one undoable archive exists (drives `u`-key gating in keyboard hook). */
  hasUndoableArchive: boolean;
  /**
   * Optimistic-archive an issue. Removes from all 3 inbox SWR caches,
   * fires server POST. On success pushes onto undo stack. On error rolls
   * back the cache snapshot.
   */
  archive: (issueId: string) => Promise<void>;
  /**
   * Pop the most recent archived id off the undo stack and unarchive it
   * server-side. Re-validates inbox SWR caches. Caller wires this to
   * `useInboxKeyboardNav`'s `onUndoArchive` callback.
   */
  undoArchive: () => Promise<void>;
  /** Mark a single issue as read (server POST + cache update via useReadInboxItems pattern). */
  markRead: (issueId: string) => Promise<void>;
  /** Mark a single issue as unread. */
  markUnread: (issueId: string) => Promise<void>;
}

export function useInboxArchiveStack(): UseInboxArchiveStackResult {
  const { post } = useTeamsApi();
  const { cache, mutate } = useSWRConfig();
  const [archivingIssueIds, setArchivingIssueIds] = useState<Set<string>>(
    new Set(),
  );
  const [undoStack, setUndoStack] = useState<string[]>([]);

  const archive = useCallback(
    async (issueId: string) => {
      setArchivingIssueIds((prev) => {
        if (prev.has(issueId)) return prev;
        const next = new Set(prev);
        next.add(issueId);
        return next;
      });

      // Snapshot for rollback on error.
      const snapshots = INBOX_KEYS.map((key) => {
        const fullKey = swrKey(key);
        return {
          key,
          fullKey,
          prev: cache.get(fullKey)?.data as IssueListData | undefined,
        };
      });

      // Optimistic update — remove the issue from each cached array.
      for (const { key } of snapshots) {
        mutate(
          swrKey(key),
          (prev?: IssueListData) => withoutId(prev, issueId),
          { revalidate: false },
        );
      }

      try {
        // BFF: POST /teams/inbox/{id}/archive (path here without "/teams" prefix; useTeamsApi adds it)
        await post(`/inbox/${issueId}/archive`, {});
        // Success: push onto undo stack via functional updater (avoids stale closure).
        setUndoStack((prev) => [...prev, issueId]);
      } catch (err) {
        // Rollback the cache snapshots.
        for (const { key, prev } of snapshots) {
          mutate(swrKey(key), prev, { revalidate: false });
        }
        // Settle on error: pull fresh server state to converge after rollback.
        for (const key of INBOX_KEYS) {
          mutate(swrKey(key));
        }
        throw err;
      } finally {
        setArchivingIssueIds((prev) => {
          if (!prev.has(issueId)) return prev;
          const next = new Set(prev);
          next.delete(issueId);
          return next;
        });
      }
    },
    [cache, mutate, post],
  );

  const undoArchive = useCallback(async () => {
    // Pop the top of the stack via a functional updater so we read the
    // latest committed state without keeping a ref mirror. We wrap the
    // setter in flushSync to force the updater to run during the dispatch
    // call (vs being queued for the next commit phase) — this is the
    // canonical pattern for "read latest state synchronously inside an
    // async callback" once a ref mirror is off the table. The popped id
    // is captured via `issueId` closure inside the updater.
    let issueId: string | undefined;
    flushSync(() => {
      setUndoStack((prev) => {
        if (prev.length === 0) return prev;
        issueId = prev[prev.length - 1];
        return prev.slice(0, -1);
      });
    });
    if (!issueId) return;

    try {
      await post(`/inbox/${issueId}/unarchive`, {});
    } catch (err) {
      // Restore the popped id back onto the stack.
      setUndoStack((prev) => [...prev, issueId!]);
      // Settle on error: pull fresh server state to converge.
      for (const key of INBOX_KEYS) {
        mutate(swrKey(key));
      }
      throw err;
    }
  }, [mutate, post]);

  const markRead = useCallback(
    async (issueId: string) => {
      // Snapshot for rollback on error.
      const snapshots = INBOX_KEYS.map((key) => {
        const fullKey = swrKey(key);
        return {
          key,
          fullKey,
          prev: cache.get(fullKey)?.data as IssueListData | undefined,
        };
      });
      // Optimistic: flip unread → false in cache.
      for (const { key } of snapshots) {
        mutate(
          swrKey(key),
          (prev?: IssueListData) => setIssueReadFlag(prev, issueId, false),
          { revalidate: false },
        );
      }
      try {
        await post(`/inbox/${issueId}/mark-read`, {});
      } catch (err) {
        // Rollback.
        for (const { key, prev } of snapshots) {
          mutate(swrKey(key), prev, { revalidate: false });
        }
        // Settle on error.
        for (const key of INBOX_KEYS) {
          mutate(swrKey(key));
        }
        throw err;
      }
    },
    [cache, mutate, post],
  );

  const markUnread = useCallback(
    async (issueId: string) => {
      // Snapshot for rollback on error.
      const snapshots = INBOX_KEYS.map((key) => {
        const fullKey = swrKey(key);
        return {
          key,
          fullKey,
          prev: cache.get(fullKey)?.data as IssueListData | undefined,
        };
      });
      // Optimistic: flip unread → true in cache.
      for (const { key } of snapshots) {
        mutate(
          swrKey(key),
          (prev?: IssueListData) => setIssueReadFlag(prev, issueId, true),
          { revalidate: false },
        );
      }
      try {
        await post(`/inbox/${issueId}/mark-unread`, {});
      } catch (err) {
        // Rollback.
        for (const { key, prev } of snapshots) {
          mutate(swrKey(key), prev, { revalidate: false });
        }
        // Settle on error.
        for (const key of INBOX_KEYS) {
          mutate(swrKey(key));
        }
        throw err;
      }
    },
    [cache, mutate, post],
  );

  return {
    archivingIssueIds,
    hasUndoableArchive: undoStack.length > 0,
    archive,
    undoArchive,
    markRead,
    markUnread,
  };
}
