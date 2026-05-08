"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

export type DrawerView = "browse" | "mine" | "publish";

export interface DrawerState {
  isOpen: boolean;
  view: DrawerView;
  prevView?: DrawerView;
  publishAgentId?: string;
  freshlyInstalledAgentId: string | null;
  toast: { agentName: string; agentId: string } | null;
}

export interface OpenOptions {
  view: DrawerView;
  agentId?: string;
}

interface DrawerContextValue {
  state: DrawerState;
  open: (opts: OpenOptions) => void;
  close: () => void;
  setView: (view: DrawerView) => void;
  showInstallToast: (toast: { agentName: string; agentId: string }) => void;
  dismissToast: () => void;
  setFreshlyInstalled: (agentId: string | null) => void;
}

const Ctx = createContext<DrawerContextValue | null>(null);

const INITIAL: DrawerState = {
  isOpen: false,
  view: "browse",
  freshlyInstalledAgentId: null,
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
      prevView: opts.view === "publish" ? prev.view : prev.prevView,
    }));
  }, []);

  const close = useCallback(() => {
    setState((prev) => ({
      ...prev,
      isOpen: false,
      publishAgentId: undefined,
      // toast persists across drawer close — it has its own dismiss timer
    }));
  }, []);

  const setView = useCallback((view: DrawerView) => {
    setState((prev) => ({
      ...prev,
      view,
      prevView: view === "publish" ? prev.view : prev.prevView,
      publishAgentId: view === "publish" ? prev.publishAgentId : undefined,
    }));
  }, []);

  const showInstallToast = useCallback((toast: { agentName: string; agentId: string }) => {
    setState((prev) => ({ ...prev, toast }));
  }, []);

  const dismissToast = useCallback(() => {
    setState((prev) => ({ ...prev, toast: null }));
  }, []);

  const setFreshlyInstalled = useCallback((agentId: string | null) => {
    setState((prev) => ({ ...prev, freshlyInstalledAgentId: agentId }));
  }, []);

  const value = useMemo<DrawerContextValue>(
    () => ({ state, open, close, setView, showInstallToast, dismissToast, setFreshlyInstalled }),
    [state, open, close, setView, showInstallToast, dismissToast, setFreshlyInstalled],
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
