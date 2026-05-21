// apps/frontend/src/components/teams/inbox/hooks/useReadInboxItems.ts

// Ported from upstream Paperclip's hooks/useInboxBadge.ts (useReadInboxItems
// only — useDismissedInboxAlerts / useInboxDismissals are not yet ported).
// (paperclip/ui/src/hooks/useInboxBadge.ts) (MIT, (c) 2025 Paperclip AI).

import { useCallback, useEffect, useState } from "react";
import {
  inboxStorageKey,
  loadReadInboxItems,
  saveReadInboxItems,
} from "@/components/teams/shared/lib/inboxStorage";

export interface UseReadInboxItemsResult {
  /** Current set of read item IDs. */
  readItemKeys: Set<string>;
  /** True if the given id is in the read set. */
  isRead: (id: string) => boolean;
  /** Mark a single id as read (state + persisted). */
  markRead: (id: string) => void;
  /** Remove a single id from the read set (state + persisted). */
  markUnread: (id: string) => void;
  /** Mark many ids as read in one batched update + single save. */
  markManyRead: (ids: Iterable<string>) => void;
  /** Clear the entire read set (used by "Mark all as read"). */
  clearAll: () => void;
}

/**
 * React hook that tracks the set of read-marked inbox item IDs for a company,
 * persisting to localStorage and syncing across tabs via the `storage` event.
 *
 * When `companyId` is `null` (loading state), returns an empty Set + no-op
 * mutators so callers can render unconditionally.
 *
 * Same-tab updates are reflected via React state; only cross-tab updates
 * arrive via the `storage` event listener (per spec, the event fires only on
 * other tabs, not the originating tab).
 */
export function useReadInboxItems(
  companyId: string | null,
): UseReadInboxItemsResult {
  const [readItemKeys, setReadItemKeys] = useState<Set<string>>(() =>
    companyId ? loadReadInboxItems(companyId) : new Set(),
  );
  // Track the companyId the current state was loaded for. When it changes,
  // reset state inline during render — this avoids the
  // `react-hooks/set-state-in-effect` pattern that triggers cascading renders.
  // See https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [loadedCompanyId, setLoadedCompanyId] = useState<string | null>(
    companyId,
  );
  if (companyId !== loadedCompanyId) {
    setLoadedCompanyId(companyId);
    setReadItemKeys(companyId ? loadReadInboxItems(companyId) : new Set());
  }

  // Cross-tab sync: another tab wrote to the same key.
  useEffect(() => {
    if (!companyId) return;
    const key = inboxStorageKey(companyId, "read-items");
    const handler = (event: StorageEvent) => {
      if (event.key !== key) return;
      setReadItemKeys(loadReadInboxItems(companyId));
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [companyId]);

  const markRead = useCallback(
    (id: string) => {
      if (!companyId) return;
      setReadItemKeys((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        saveReadInboxItems(companyId, next);
        return next;
      });
    },
    [companyId],
  );

  const markUnread = useCallback(
    (id: string) => {
      if (!companyId) return;
      setReadItemKeys((prev) => {
        if (!prev.has(id)) return prev;
        const next = new Set(prev);
        next.delete(id);
        saveReadInboxItems(companyId, next);
        return next;
      });
    },
    [companyId],
  );

  const markManyRead = useCallback(
    (ids: Iterable<string>) => {
      if (!companyId) return;
      setReadItemKeys((prev) => {
        const next = new Set(prev);
        let changed = false;
        for (const id of ids) {
          if (!next.has(id)) {
            next.add(id);
            changed = true;
          }
        }
        if (!changed) return prev;
        saveReadInboxItems(companyId, next);
        return next;
      });
    },
    [companyId],
  );

  const clearAll = useCallback(() => {
    if (!companyId) return;
    setReadItemKeys((prev) => {
      if (prev.size === 0) return prev;
      const next = new Set<string>();
      saveReadInboxItems(companyId, next);
      return next;
    });
  }, [companyId]);

  const isRead = useCallback(
    (id: string) => readItemKeys.has(id),
    [readItemKeys],
  );

  return { readItemKeys, isRead, markRead, markUnread, markManyRead, clearAll };
}
