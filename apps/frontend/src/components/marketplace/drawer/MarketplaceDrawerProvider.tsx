"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import type { Listing } from "@/lib/marketplace/types";

export type DrawerView = "browse" | "mine" | "publish" | "detail";

/** ACP coding-harness delegation outcome surfaced under the install toast.
 * "enabled" — the buyer's container got ACP turned on (delegation live).
 * "unavailable" — the listing wants ACP but the buyer's provider can't run a
 * harness yet (agent still deploys, just no delegation). Absent = not an ACP
 * listing, no note shown. */
export type AcpNote = "enabled" | "unavailable";

export interface DrawerState {
  isOpen: boolean;
  view: DrawerView;
  prevView?: DrawerView;
  publishAgentId?: string;
  /** Set by MyListingsView when the seller clicks "Edit" on a draft. The
   * publish wizard reads it on mount, pre-fills its form, locks the agent
   * picker, and switches its submit path to PATCH → artifact → submit. */
  editListing?: Listing | null;
  /** Set by openListingDetail when the seller drills into a listing row.
   * The detail view reads it to fetch + render the listing; clearing it
   * on view changes keeps a stale id from leaking back into the page. */
  detailListingId?: string | null;
  freshlyInstalledAgentId: string | null;
  /** Slugs hired in THIS session, set synchronously on a successful Hire.
   * The catalog's own ``installedSlugs`` lags a deploy (it derives from
   * template sidecars that land via the container's chokidar reload), so
   * BrowseView unions this optimistic overlay in — without it a buyer who
   * re-opens the drawer right after hiring still sees "Hire" and can stack
   * a duplicate install. */
  installedSlugs: Set<string>;
  /** ``error`` set ⇒ the toast renders its failure variant (no "Open in
   * chat"). Success toasts leave it undefined. */
  toast: {
    agentName: string;
    agentId: string;
    acpNote?: AcpNote;
    /** How many webhook automations the hire left for the buyer to wire (the
     * deploy arms none — each binds a resource only the buyer can name). */
    automationsPending?: number;
    error?: string;
    sticky?: boolean;
  } | null;
}

export interface OpenOptions {
  view: DrawerView;
  agentId?: string;
  editListing?: Listing;
}

interface DrawerContextValue {
  state: DrawerState;
  open: (opts: OpenOptions) => void;
  close: () => void;
  setView: (view: DrawerView) => void;
  startEditListing: (listing: Listing) => void;
  openListingDetail: (listingId: string) => void;
  showInstallToast: (toast: {
    agentName: string;
    agentId: string;
    acpNote?: AcpNote;
    automationsPending?: number;
  }) => void;
  /** Surface a failed Hire. Without this a rejected deploy was an unhandled
   * promise rejection — the dialog just froze ("the screen bugging out"). */
  showErrorToast: (message: string, opts?: { sticky?: boolean }) => void;
  dismissToast: () => void;
  setFreshlyInstalled: (agentId: string | null) => void;
  /** Record a just-hired slug so its card flips to "Hired ✓" immediately. */
  markInstalled: (slug: string) => void;
}

const Ctx = createContext<DrawerContextValue | null>(null);

const INITIAL: DrawerState = {
  isOpen: false,
  view: "browse",
  freshlyInstalledAgentId: null,
  installedSlugs: new Set(),
  toast: null,
};

export function MarketplaceDrawerProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<DrawerState>(INITIAL);

  const open = useCallback((opts: OpenOptions) => {
    setState((prev) => ({
      ...prev,
      isOpen: true,
      view: opts.view,
      publishAgentId: opts.agentId,
      editListing: opts.editListing ?? null,
      prevView: opts.view === "publish" ? prev.view : prev.prevView,
      // Opening into any non-detail view drops a stale detail id so the
      // detail render branch can't flash old content.
      detailListingId: opts.view === "detail" ? prev.detailListingId : null,
    }));
  }, []);

  const close = useCallback(() => {
    setState((prev) => ({
      ...prev,
      isOpen: false,
      publishAgentId: undefined,
      editListing: null,
      detailListingId: null,
      // toast persists across drawer close — it has its own dismiss timer
    }));
  }, []);

  const setView = useCallback((view: DrawerView) => {
    setState((prev) => ({
      ...prev,
      view,
      prevView: view === "publish" ? prev.view : prev.prevView,
      publishAgentId: view === "publish" ? prev.publishAgentId : undefined,
      // Leaving the publish view also clears the editListing — re-entering
      // publish with the "+ New listing" button must start fresh.
      editListing: view === "publish" ? prev.editListing : null,
      // Same idea for detail: navigating away (e.g. Back to my listings)
      // should not preserve the id — a future detail open re-supplies it.
      detailListingId: view === "detail" ? prev.detailListingId : null,
    }));
  }, []);

  const startEditListing = useCallback((listing: Listing) => {
    setState((prev) => ({
      ...prev,
      // Without isOpen=true, callers from a closed-drawer surface (e.g.
      // /marketplace/listing/[slug]/manage) update state but never render
      // the wizard. On /chat the drawer is already open so this is a no-op.
      isOpen: true,
      view: "publish",
      prevView: prev.view,
      editListing: listing,
      publishAgentId: listing.agent_id ?? listing.manifest_json?.agent_id,
      detailListingId: null,
    }));
  }, []);

  const openListingDetail = useCallback((listingId: string) => {
    setState((prev) => ({
      ...prev,
      isOpen: true,
      view: "detail",
      prevView: prev.view,
      detailListingId: listingId,
      // Detail is a read-only inspector — clear any in-flight publish edit.
      editListing: null,
      publishAgentId: undefined,
    }));
  }, []);

  const showInstallToast = useCallback(
    (toast: { agentName: string; agentId: string; acpNote?: AcpNote; automationsPending?: number }) => {
      setState((prev) => ({ ...prev, toast }));
    },
    [],
  );

  const showErrorToast = useCallback((message: string, opts?: { sticky?: boolean }) => {
    setState((prev) => ({
      ...prev,
      toast: { agentName: "", agentId: "", error: message, sticky: opts?.sticky },
    }));
  }, []);

  const dismissToast = useCallback(() => {
    setState((prev) => ({ ...prev, toast: null }));
  }, []);

  const setFreshlyInstalled = useCallback((agentId: string | null) => {
    setState((prev) => ({ ...prev, freshlyInstalledAgentId: agentId }));
  }, []);

  const markInstalled = useCallback((slug: string) => {
    setState((prev) => {
      if (prev.installedSlugs.has(slug)) return prev;
      const next = new Set(prev.installedSlugs);
      next.add(slug);
      return { ...prev, installedSlugs: next };
    });
  }, []);

  const value = useMemo<DrawerContextValue>(
    () => ({
      state,
      open,
      close,
      setView,
      startEditListing,
      openListingDetail,
      showInstallToast,
      showErrorToast,
      dismissToast,
      setFreshlyInstalled,
      markInstalled,
    }),
    [
      state,
      open,
      close,
      setView,
      startEditListing,
      openListingDetail,
      showInstallToast,
      showErrorToast,
      dismissToast,
      setFreshlyInstalled,
      markInstalled,
    ],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useMarketplaceDrawer(): DrawerContextValue {
  const ctx = useContext(Ctx);
  if (!ctx) {
    throw new Error("useMarketplaceDrawer must be used inside <MarketplaceDrawerProvider>");
  }
  return ctx;
}
