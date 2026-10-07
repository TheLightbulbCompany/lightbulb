"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import { SupportTicketDialog } from "./SupportTicketDialog";

/** Captured error to attach to a ticket opened from an error surface. */
export interface SupportError {
  message: string;
  stack?: string;
}

export interface OpenSupportOptions {
  /** Pre-select the topic (defaults to "Bug report" for error-originated opens). */
  category?: string;
  /** Pre-fill the message body (e.g. an integration access request). */
  message?: string;
  /** Error details to attach (from an error toast / boundary / catch block). */
  error?: SupportError;
  /** Backend X-Request-ID for the failing request, pinned to the ticket. */
  requestId?: string | null;
}

interface SupportContextValue {
  openSupport: (opts?: OpenSupportOptions) => void;
  closeSupport: () => void;
}

const SupportContext = createContext<SupportContextValue | null>(null);

/**
 * Owns the single shared support dialog instance and exposes `openSupport`.
 *
 * Mounted high in the tree (below Clerk, inside PostHogProvider) so any surface
 * — the Help button, an error toast's Report button, the ErrorBoundary — opens
 * the *same* dialog rather than each shipping its own. The `<Toaster/>` is
 * mounted as a sibling INSIDE this provider so a toast's `<ReportButton/>` (a
 * custom-JSX sonner action) can call `useSupport()`.
 */
export function SupportProvider({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const [preset, setPreset] = useState<OpenSupportOptions | null>(null);
  // Bumped on every open so the dialog remounts with a fresh `key` — each open
  // re-initializes category/message/state from the current preset, no effect.
  const [instanceKey, setInstanceKey] = useState(0);

  const openSupport = useCallback((opts?: OpenSupportOptions) => {
    setPreset(opts ?? null);
    setInstanceKey((k) => k + 1);
    setOpen(true);
  }, []);

  const closeSupport = useCallback(() => setOpen(false), []);

  const value = useMemo<SupportContextValue>(() => ({ openSupport, closeSupport }), [openSupport, closeSupport]);

  return (
    <SupportContext.Provider value={value}>
      {children}
      <SupportTicketDialog
        key={instanceKey}
        open={open}
        onClose={closeSupport}
        presetCategory={preset?.category}
        presetMessage={preset?.message}
        presetError={preset?.error}
        presetRequestId={preset?.requestId}
      />
    </SupportContext.Provider>
  );
}

export function useSupport(): SupportContextValue {
  const ctx = useContext(SupportContext);
  if (!ctx) {
    throw new Error("useSupport must be used within a <SupportProvider>");
  }
  return ctx;
}
