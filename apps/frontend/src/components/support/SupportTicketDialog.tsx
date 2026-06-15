"use client";

import { usePathname } from "next/navigation";
import { useState } from "react";

import { useApi } from "@/lib/api";

export const SUPPORT_CATEGORIES = [
  "Bug report",
  "Feature request",
  "Billing question",
  "Account issue",
  "GooseTown",
  "Other",
] as const;

type SubmitState = "idle" | "sending" | "sent" | "error";

export interface SupportTicketDialogProps {
  open: boolean;
  onClose: () => void;
  /** Pre-fill the category (e.g. "Bug report" from the ErrorBoundary). */
  presetCategory?: string;
  /** When opened from an error surface, the captured error to attach. */
  presetError?: { message: string; stack?: string };
}

/**
 * Shared support-ticket modal. Collects the user's message + benign client
 * context and POSTs to the backend, which enriches it server-side and emails
 * it to the support inbox. Used by /support, the ErrorBoundary, the app-shell
 * Help button, and error toasts.
 */
export function SupportTicketDialog({ open, onClose, presetCategory, presetError }: SupportTicketDialogProps) {
  const api = useApi();
  const pathname = usePathname();
  const [category, setCategory] = useState(presetCategory ?? "Bug report");
  const [message, setMessage] = useState("");
  const [state, setState] = useState<SubmitState>("idle");

  if (!open) return null;

  const submit = async () => {
    if (!message.trim()) return;
    setState("sending");
    try {
      await api.post("/support/tickets", {
        message,
        category,
        route: pathname,
        user_agent: typeof navigator !== "undefined" ? navigator.userAgent : null,
        app_sha: process.env.NEXT_PUBLIC_APP_SHA ?? null,
        error_message: presetError?.message ?? null,
        error_stack: presetError?.stack ?? null,
      });
      setState("sent");
    } catch {
      setState("error");
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-lg bg-white p-6 shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        {state === "sent" ? (
          <div className="text-center">
            <h2 className="mb-2 text-lg font-semibold">Thanks — we got it</h2>
            <p className="mb-6 text-sm text-gray-600">
              Your message is on its way to our team. We&rsquo;ll reply to your email.
            </p>
            <button
              type="button"
              onClick={onClose}
              className="rounded-md bg-black px-4 py-2 text-sm text-white hover:bg-gray-800"
            >
              Close
            </button>
          </div>
        ) : (
          <>
            <div className="mb-4 flex items-center justify-between">
              <h2 className="text-lg font-semibold">Contact support</h2>
              <button type="button" onClick={onClose} aria-label="Close" className="text-gray-400 hover:text-gray-600">
                ✕
              </button>
            </div>

            {presetError ? (
              <p className="mb-3 rounded-md bg-red-50 px-3 py-2 text-xs text-red-700">
                We&rsquo;ll attach the error details automatically.
              </p>
            ) : null}

            <label className="mb-1 block text-sm font-medium text-gray-700" htmlFor="support-category">
              Topic
            </label>
            <select
              id="support-category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="mb-4 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
            >
              {SUPPORT_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>

            <label className="mb-1 block text-sm font-medium text-gray-700" htmlFor="support-message">
              Message
            </label>
            <textarea
              id="support-message"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              rows={5}
              placeholder="Describe what happened…"
              className="mb-4 w-full rounded-md border border-gray-300 px-3 py-2 text-sm"
            />

            {state === "error" ? (
              <p className="mb-3 text-xs text-red-600">
                Couldn&rsquo;t send — please try again, or email support@isol8.co directly.
              </p>
            ) : null}

            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={onClose}
                className="rounded-md border border-gray-300 px-4 py-2 text-sm hover:bg-gray-50"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={submit}
                disabled={state === "sending" || !message.trim()}
                className="rounded-md bg-black px-4 py-2 text-sm text-white hover:bg-gray-800 disabled:opacity-50"
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
