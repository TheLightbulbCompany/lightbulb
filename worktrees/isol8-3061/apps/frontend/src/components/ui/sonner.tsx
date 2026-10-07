"use client";

import { Toaster as SonnerToaster } from "sonner";

/**
 * App toast surface — the single source of toast UX truth.
 *
 * - `bottom-right`: clears the persistent top banners (out-of-credits, trial,
 *   container-update) and the HealthIndicator dot.
 * - `theme="light"`: the app is light-only; we deliberately do NOT pull in
 *   next-themes (the shadcn template's dependency).
 * - `richColors` + `closeButton`.
 * - `visibleToasts={5}`: excess queue. Sticky toasts (failures, pending
 *   approvals) hold their slot until dismissed, so the ceiling must leave
 *   headroom for new notifications to still surface.
 *
 * Durations are set per-call (see `notify.tsx`): error toasts are STICKY
 * (errors must not auto-dismiss); success/info auto-dismiss.
 *
 * a11y: sonner's region is hard-coded `aria-live="polite"` (no assertive path,
 * no per-toast role). That is accepted; the residual risk is critical errors
 * being under-announced — if one ever needs assertive announcement, render a
 * separate `role="alert"` node rather than expecting sonner to emit it.
 */
export function Toaster() {
  return <SonnerToaster position="bottom-right" theme="light" richColors closeButton visibleToasts={5} />;
}
