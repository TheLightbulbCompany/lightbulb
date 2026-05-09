// Scoped-per-company list/board view-mode persistence.
// Mirrors upstream Paperclip's IssuesList view-mode storage pattern
// (paperclip/ui/src/components/IssuesList.tsx:568) (MIT, (c) 2025 Paperclip AI).

"use client";

import { useCallback, useEffect, useState } from "react";

export type InboxViewMode = "list" | "board";

const KEY_PREFIX = "isol8:teams:inbox-view-mode:";
const VALID: readonly InboxViewMode[] = ["list", "board"];

function isViewMode(v: string | null): v is InboxViewMode {
  return v !== null && (VALID as readonly string[]).includes(v);
}

export function useInboxViewMode(companyId: string | null): {
  viewMode: InboxViewMode;
  setViewMode: (mode: InboxViewMode) => void;
} {
  const [viewMode, setViewModeState] = useState<InboxViewMode>("list");

  useEffect(() => {
    if (!companyId) {
      setViewModeState("list");
      return;
    }
    const stored = window.localStorage.getItem(KEY_PREFIX + companyId);
    setViewModeState(isViewMode(stored) ? stored : "list");
  }, [companyId]);

  const setViewMode = useCallback(
    (mode: InboxViewMode) => {
      if (!companyId) return; // no key to scope to; treat as no-op
      window.localStorage.setItem(KEY_PREFIX + companyId, mode);
      setViewModeState(mode);
    },
    [companyId],
  );

  return { viewMode, setViewMode };
}
