"use client";

import { usePathname } from "next/navigation";
import { useState } from "react";

import { capture } from "@/lib/analytics";
import { useApi } from "@/lib/api";
import { SUPPORT_EMAIL } from "@/lib/domain";
import { getRecentRequestIds } from "@/lib/requestIdLog";

export const SUPPORT_CATEGORIES = [
  "Bug report",
  "Feature request",
  "Integration request",
  "Billing question",
  "Account issue",
  "GooseTown",
  "Other",
] as const;

type SubmitState = "idle" | "sending" | "sent" | "received" | "error";

export interface SupportTicketDialogProps {
  open: boolean;
  onClose: () => void;
  /** Pre-fill the category (e.g. "Bug report" from the ErrorBoundary). */
  presetCategory?: string;
  /** Pre-fill the message body (e.g. "I'd like to use Instagram."). */
  presetMessage?: string;
  /** When opened from an error surface, the captured error to attach. */
  presetError?: { message: string; stack?: string };
  /** Backend X-Request-ID for the failing request (error surfaces only). */
  presetRequestId?: string | null;
}

/**
 * Shared support-ticket modal. Collects the user's message + benign client
 * context and POSTs to the backend, which enriches it server-side and emails
 * it to the support inbox. Used by /support, the ErrorBoundary, the app-shell
 * Help button, and error toasts.
 */
export function SupportTicketDialog({ open, onClose, presetCategory, presetMessage, presetError, presetRequestId }: SupportTicketDialogProps) {
  const api = useApi();
  const pathname = usePathname();
  // State initializes once per mount. The provider remounts this dialog (via a
  // changing `key`) on every open, so each open starts fresh from the current
  // preset — no reset effect needed, and no stale category/message carried over.
  const [category, setCategory] = useState(presetCategory ?? "Bug report");
  const [message, setMessage] = useState(presetMessage ?? "");
  const [state, setState] = useState<SubmitState>("idle");

  if (!open) return null;

  const submit = async () => {
    if (!message.trim()) return;
    setState("sending");
    try {
      const res = await api.post("/support/tickets", {
        message,
        category,
        route: pathname,
        user_agent: typeof navigator !== "undefined" ? navigator.userAgent : null,
        app_sha: process.env.NEXT_PUBLIC_APP_SHA ?? null,
        error_message: presetError?.message ?? null,
        error_stack: presetError?.stack ?? null,
        // The failing request first, then the recent-request ring buffer
        // (newest-first), de-duped and capped to the backend's max_length=20.
        recent_request_ids: Array.from(
          new Set([presetRequestId, ...getRecentRequestIds()].filter(Boolean) as string[]),
        ).slice(0, 20),
      });
      // The route 200s either way: "sent" = the email left, "received" = email
      // is disabled or the send failed and the ticket exists only in the backend
      // log. Promising a reply in the second case is a lie the user acts on, so
      // the two get different copy.
      const delivered = (res as { status?: string } | null)?.status !== "received";
      capture("support_ticket_submitted", {
        category,
        route: pathname,
        from_error: !!presetError,
        delivered,
      });
      setState(delivered ? "sent" : "received");
    } catch {
      setState("error");
    }
  };

  return (
    <div
      className="fixed inset-0 z-(--z-overlay) flex items-center justify-center bg-[color-mix(in_srgb,var(--midnight)_50%,transparent)] p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg bg-cream p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {state === "sent" || state === "received" ? (
          <div className="text-center">
            <h2 className="mb-2 font-editorial-serif text-title">
              {state === "sent" ? "Thanks — we got it" : "Couldn’t send that"}
            </h2>
            <p className="mb-6 text-sm text-[var(--midnight-muted)]">
              {state === "sent" ? (
                <>Your message is on its way to our team. We&rsquo;ll reply to your email.</>
              ) : (
                // Don't claim a record the user can rely on: delivery is the only
                // capture path, so on failure the honest move is to hand them one
                // that doesn't depend on our mail working.
                <>
                  Our email delivery is having trouble, so we can&rsquo;t promise this reached
                  us. Please send it straight to {SUPPORT_EMAIL} and we&rsquo;ll pick it up
                  there.
                </>
              )}
            </p>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md bg-midnight px-4 py-2 text-sm text-cream hover:bg-[var(--midnight-soft)]"
            >
              Close
            </button>
          </div>
        ) : (
          <>
            <div className="mb-4 flex items-center justify-between">
              <h2 className="font-editorial-serif text-title">Contact support</h2>
              <button type="button" onClick={onClose} aria-label="Close" className="text-[var(--midnight-muted)] hover:text-midnight">
                ✕
              </button>
            </div>

            {presetError ? (
              <p className="border border-err mb-3 rounded-lg bg-cream px-3 py-2 text-ui text-err">
                We&rsquo;ll attach the error details automatically.
              </p>
            ) : null}

            <label className="mb-1 block text-sm font-medium text-midnight" htmlFor="support-category">
              Topic
            </label>
            <select
              id="support-category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="mb-4 w-full rounded-md border border-edge px-3 py-2 text-sm"
            >
              {SUPPORT_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>

            <label className="mb-1 block text-sm font-medium text-midnight" htmlFor="support-message">
              Message
            </label>
            <textarea
              id="support-message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={5}
              placeholder="Describe what happened…"
              className="mb-4 w-full rounded-md border border-edge px-3 py-2 text-sm"
            />

            {state === "error" ? (
              <p className="mb-3 text-xs text-err">
                Couldn&rsquo;t send — please try again, or email {SUPPORT_EMAIL} directly.
              </p>
            ) : null}

            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-md border border-edge px-4 py-2 text-sm hover:bg-cream-deep"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={state === "sending" || !message.trim()}
                className="rounded-md bg-midnight px-4 py-2 text-sm text-cream hover:bg-[var(--midnight-soft)] disabled:opacity-50"
              >
                {state === "sending" ? "Sending…" : "Send"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
