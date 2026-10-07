"use client";

import { useEffect } from "react";
import { useMarketplaceDrawer } from "./MarketplaceDrawerProvider";

export function MarketplaceToast() {
  const { state, dismissToast } = useMarketplaceDrawer();
  const toast = state.toast;

  useEffect(() => {
    if (!toast) return;
    // Sticky toasts (e.g. "retrying shortly…") outlive the 5.5s timer — the
    // pending auto-retry fires ~20s later and auto-dismissing its notice left
    // the buyer staring at a silent dialog wondering if anything was queued.
    if (toast.sticky) return;
    const handle = setTimeout(() => dismissToast(), 5500);
    return () => clearTimeout(handle);
  }, [toast, dismissToast]);

  if (!toast) return null;

  function handleOpen() {
    if (!toast) return;
    window.dispatchEvent(new CustomEvent("selectAgent", { detail: { agentId: toast.agentId } }));
    dismissToast();
  }

  // Failure variant: a Hire that errored. No "Open in chat" (there's no
  // agent), amber surface, manual dismiss.
  if (toast.error) {
    return (
      // --err-reversed, not --err: the toast paints its own midnight fill while
      // the document is still on :root, so the border is a state mark on
      // midnight. --err there is the cream-ground red — 2.4:1, under 1.4.11's 3:1.
      <div
        data-testid="toast-error"
        role="alert"
        className="fixed bottom-8 left-1/2 -translate-x-1/2 z-[100] bg-[var(--midnight)] text-[var(--cream)] border border-[var(--err-reversed)] rounded-xl px-5 py-3.5 shadow-xl"
      >
        <div className="flex items-center gap-3.5 text-sm">
          <span>{toast.error}</span>
          <button
            type="button"
            onClick={() => dismissToast()}
            className="bg-[var(--cream)]/20 hover:bg-[var(--cream)]/30 px-3 py-1.5 rounded-full text-ui font-semibold"
          >
            Dismiss
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="fixed bottom-8 left-1/2 -translate-x-1/2 z-[100] bg-[var(--midnight)] text-[var(--cream)] rounded-xl px-5 py-3.5 shadow-xl">
      <div className="flex items-center gap-3.5 text-sm">
        <span>
          ✓ <strong>{toast.agentName} installed</strong> in your workspace.
        </span>
        <button
          type="button"
          onClick={handleOpen}
          className="bg-[var(--cream)]/20 hover:bg-[var(--cream)]/30 px-3 py-1.5 rounded-full text-ui font-semibold"
        >
          Open in chat →
        </button>
      </div>
      {!!toast.automationsPending && (
        <p
          data-testid="toast-automations-pending"
          className="mt-2 text-xs text-[var(--cream)]/85 leading-snug"
        >
          {toast.automationsPending === 1
            ? "1 automation needs wiring"
            : `${toast.automationsPending} automations need wiring`}{" "}
          — each one listens to an account only you can connect. Open Automations to finish
          setting them up.
        </p>
      )}
      {toast.acpNote === "enabled" && (
        <p data-testid="toast-acp-enabled" className="mt-2 text-xs text-[var(--cream)]/85 leading-snug">
          This agent can delegate coding tasks to a Claude Code / Codex harness
          (matched to your provider).
        </p>
      )}
      {toast.acpNote === "unavailable" && (
        <p
          data-testid="toast-acp-unavailable"
          className="mt-2 text-xs text-[var(--cream)]/85 leading-snug"
        >
          This agent wants a coding harness, but your current provider can&apos;t
          run one yet.
        </p>
      )}
    </div>
  );
}
