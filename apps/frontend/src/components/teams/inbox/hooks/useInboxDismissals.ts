// apps/frontend/src/components/teams/inbox/hooks/useInboxDismissals.ts

// Ported from upstream Paperclip's useInboxDismissals
// (paperclip/ui/src/hooks/useInboxBadge.ts @ v2026.1012.0-isol8.4) and
// inboxDismissalsApi (paperclip/ui/src/api/inboxDismissals.ts)
// (MIT, (c) 2025 Paperclip AI). The three writes, their bodies and the
// encoded restore path are verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - react-query mutations          -> useTeamsApi.post / del
//   - the dismissals list read and its optimistic cache entry
//                                    -> not ported: the desk reads `dismissal`
//                                       off each feed row and hides rows itself
//   - invalidate dismissals / sidebar badges / attention
//                                    -> one mutate over every /teams/attention key:
//                                       the badge's `/attention` AND the desk's
//                                       `/attention?includeDismissed=true&all=true`
//   - companyId                      -> dropped (the BFF pins the workspace)

"use client";

import { useCallback } from "react";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { teamsQueryKeys } from "@/components/teams/shared/queryKeys";
import { isAttentionKey } from "@/components/teams/inbox/hooks/useAttentionFeed";

export function useInboxDismissals() {
  const { post, del, mutate } = useTeamsApi();
  const base = teamsQueryKeys.inboxDismissals();
  const refresh = useCallback(() => {
    void mutate(isAttentionKey);
  }, [mutate]);

  // Settled, not just succeeded: a failed write must still re-pull the server's
  // truth so an optimistically hidden row comes back.
  const settle = useCallback(
    async (write: Promise<unknown>) => {
      try {
        await write;
      } finally {
        refresh();
      }
    },
    [refresh],
  );

  const dismiss = useCallback(
    (itemKey: string) => settle(post(base, { itemKey })),
    [settle, post, base],
  );
  const snooze = useCallback(
    (itemKey: string, snoozedUntil: string) => settle(post(base, { itemKey, kind: "snooze", snoozedUntil })),
    [settle, post, base],
  );
  const restore = useCallback(
    (itemKey: string) => settle(del(`${base}/${encodeURIComponent(itemKey)}`)),
    [settle, del, base],
  );

  return { dismiss, snooze, restore, refresh };
}
