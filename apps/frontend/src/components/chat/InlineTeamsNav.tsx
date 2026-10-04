"use client";

import { createContext, useContext } from "react";

// Bridges a Teams list panel's "Open →" affordance to the /chat shell's
// URL-driven panel router. When a panel is rendered inline under /chat,
// InlineTeamsPanel supplies a provider so rows open the detail view in-shell.
// When the same panel renders standalone under /teams (no provider), the
// hook returns null and the panel falls back to its <a href> navigation.
export interface InlineTeamsNavValue {
  openDetail: (panelKey: string, id: string) => void;
  // Detail-delete escape: a detail view whose row was just deleted/hidden
  // swaps itself for the given list panel. Committed replace-only by the shell
  // so Back never resurrects the dead row. Detail pages fall back to a direct
  // window.location rewrite when no provider is mounted (standalone/test).
  // `selfDetailId` lets the caller identify its OWN detail row: the dock
  // closes exactly that panel (focus can move between clicking Delete and
  // confirming, so the active panel is not reliably the invoker).
  leaveToList: (listPanelKey: string, selfDetailId?: string) => void;
}

const InlineTeamsNavContext = createContext<InlineTeamsNavValue | null>(null);

export function InlineTeamsNavProvider({
  value,
  children,
}: {
  value: InlineTeamsNavValue;
  children: React.ReactNode;
}): React.ReactElement {
  return (
    <InlineTeamsNavContext.Provider value={value}>
      {children}
    </InlineTeamsNavContext.Provider>
  );
}

export function useInlineTeamsNav(): InlineTeamsNavValue | null {
  return useContext(InlineTeamsNavContext);
}
