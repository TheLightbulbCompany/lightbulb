"use client";

import { useSupport, type SupportError } from "./SupportProvider";

/**
 * The "Report" action rendered inside an error toast (a sonner custom-JSX
 * action). Because it lives in the React tree under <SupportProvider> (the
 * Toaster is a sibling inside the provider), it calls useSupport() directly —
 * no opener registry. Clicking opens the shared support dialog pre-filled with
 * the error and its backend request ID.
 *
 * This is a custom-JSX action, not sonner's `{label,onClick}` form, so sonner
 * does NOT auto-dismiss the toast on click (the auto-dismiss footgun is
 * specific to `{label,onClick}`). The error toast stays until explicitly
 * closed, so the user can re-open the dialog if they dismiss it by mistake.
 */
export function ReportButton({
  error,
  requestId,
  category,
}: {
  error?: SupportError;
  requestId?: string | null;
  category?: string;
}) {
  const { openSupport } = useSupport();
  return (
    <button
      type="button"
      onClick={() => openSupport({ category: category ?? "Bug report", error, requestId })}
      className="shrink-0 whitespace-nowrap rounded-md bg-midnight px-2.5 py-1 text-xs font-semibold text-cream hover:bg-[var(--midnight-soft)]"
    >
      Report
    </button>
  );
}
