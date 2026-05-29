// apps/frontend/src/components/teams/inbox/hooks/useAcknowledgedRuns.ts
"use client";

import { useCallback, useEffect, useState } from "react";
import {
  inboxStorageKey,
  loadAckRuns,
  saveAckRuns,
} from "@/components/teams/shared/lib/inboxStorage";

export interface UseAcknowledgedRunsResult {
  acknowledgedRunIds: Set<string>;
  isAcked: (id: string) => boolean;
  ack: (id: string) => void;
}

/**
 * Per-company set of acknowledged (dismissed-from-band) failed-run IDs,
 * persisted to localStorage and synced across tabs. Mirrors
 * useReadInboxItems. `null` companyId yields an empty set + no-op `ack`.
 */
export function useAcknowledgedRuns(
  companyId: string | null,
): UseAcknowledgedRunsResult {
  const [ackIds, setAckIds] = useState<Set<string>>(() =>
    companyId ? loadAckRuns(companyId) : new Set(),
  );

  // Reset inline during render when companyId changes (avoids the
  // set-state-in-effect render flash). Same pattern as useReadInboxItems.
  const [loadedCompanyId, setLoadedCompanyId] = useState<string | null>(companyId);
  if (companyId !== loadedCompanyId) {
    setLoadedCompanyId(companyId);
    setAckIds(companyId ? loadAckRuns(companyId) : new Set());
  }

  useEffect(() => {
    if (!companyId) return;
    const key = inboxStorageKey(companyId, "ack-runs");
    const handler = (event: StorageEvent) => {
      if (event.key !== key) return;
      setAckIds(loadAckRuns(companyId));
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, [companyId]);

  const ack = useCallback(
    (id: string) => {
      if (!companyId) return;
      setAckIds((prev) => {
        if (prev.has(id)) return prev;
        const next = new Set(prev);
        next.add(id);
        saveAckRuns(companyId, next);
        return next;
      });
    },
    [companyId],
  );

  const isAcked = useCallback((id: string) => ackIds.has(id), [ackIds]);

  return { acknowledgedRunIds: ackIds, isAcked, ack };
}
