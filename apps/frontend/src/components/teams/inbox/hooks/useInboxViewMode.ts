// Scoped-per-company list/board view-mode persistence.
// Mirrors upstream Paperclip's IssuesList view-mode storage pattern
// (paperclip/ui/src/components/IssuesList.tsx:568) (MIT, (c) 2025 Paperclip AI).

"use client";

import { useCallback, useState } from "react";
import {
  loadInboxViewMode,
  saveInboxViewMode,
} from "@/components/teams/shared/lib/inboxStorage";

export type InboxViewMode = "list" | "board";

export function useInboxViewMode(companyId: string | null): {
  viewMode: InboxViewMode;
  setViewMode: (mode: InboxViewMode) => void;
} {
  // Lazy initializer — runs once on first render. Hydration-safe and avoids the
  // initial-render flash where a useEffect-driven read shows "list" for one
  // frame before flipping to the persisted value.
  const [viewMode, setViewModeState] = useState<InboxViewMode>(() =>
    companyId ? loadInboxViewMode(companyId) : "list",
  );

  // Track the companyId the current state was loaded for. When it changes,
  // reset state inline during render — same pattern as `useReadInboxItems`,
  // avoiding the `react-hooks/set-state-in-effect` flash.
  // See https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [loadedCompanyId, setLoadedCompanyId] = useState<string | null>(
    companyId,
  );
  if (companyId !== loadedCompanyId) {
    setLoadedCompanyId(companyId);
    setViewModeState(companyId ? loadInboxViewMode(companyId) : "list");
  }

  const setViewMode = useCallback(
    (mode: InboxViewMode) => {
      if (!companyId) return; // no key to scope to; treat as no-op
      saveInboxViewMode(companyId, mode);
      setViewModeState(mode);
    },
    [companyId],
  );

  return { viewMode, setViewMode };
}
