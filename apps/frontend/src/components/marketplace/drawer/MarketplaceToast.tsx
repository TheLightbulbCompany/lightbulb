"use client";

import { useEffect } from "react";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";

export function MarketplaceToast() {
  const { state, dismissToast } = useMarketplaceDrawer();
  const toast = state.toast;

  useEffect(() => {
    if (!toast) return;
    const handle = setTimeout(() => dismissToast(), 5500);
    return () => clearTimeout(handle);
  }, [toast, dismissToast]);

  if (!toast) return null;

  function handleOpen() {
    if (!toast) return;
    window.dispatchEvent(new CustomEvent("selectAgent", { detail: { agentId: toast.agentId } }));
    dismissToast();
  }

  return (
    <div className="fixed bottom-8 left-1/2 -translate-x-1/2 z-[100] bg-[#06402b] text-white rounded-2xl px-5 py-3.5 flex items-center gap-3.5 text-sm shadow-xl">
      <span>
        ✓ <strong>{toast.agentName} installed</strong> in your workspace.
      </span>
      <button
        type="button"
        onClick={handleOpen}
        className="bg-white/20 hover:bg-white/30 px-3 py-1.5 rounded-full text-[12.5px] font-semibold"
      >
        Open in chat →
      </button>
    </div>
  );
}
