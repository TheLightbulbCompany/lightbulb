"use client";

import { toast } from "sonner";

import { ReportButton } from "@/components/support/ReportButton";
import type { SupportError } from "@/components/support/SupportProvider";
import { ApiError } from "@/lib/api";

const AUTO_DISMISS_MS = 4000;

export interface NotifyOptions {
  /** Stable sonner toast id — repeated calls replace rather than stack. */
  dedupeKey?: string;
}

export interface NotifyErrorOptions extends NotifyOptions {
  /** Backend X-Request-ID for the failing request (enables the Report action). */
  requestId?: string | null;
  /** Error details attached to the ticket if the user reports it. */
  error?: SupportError;
  /** Override the support category the Report button pre-selects. */
  category?: string;
}

/** Neutral informational toast (auto-dismisses). */
export function notify(message: string, opts?: NotifyOptions): string | number {
  return toast(message, { id: opts?.dedupeKey, duration: AUTO_DISMISS_MS });
}

/** Success toast (auto-dismisses). */
export function notifySuccess(message: string, opts?: NotifyOptions): string | number {
  return toast.success(message, { id: opts?.dedupeKey, duration: AUTO_DISMISS_MS });
}

/**
 * Error toast. STICKY (`duration: Infinity`) — errors must not auto-dismiss,
 * since a toast that vanishes can be the sole notice of a failure (WCAG 2.2.1).
 * When the error carries a request ID or error details, a Report action opens
 * the support dialog pre-filled for one-click reporting.
 */
export function notifyError(message: string, opts?: NotifyErrorOptions): string | number {
  const reportable = Boolean(opts?.requestId || opts?.error);
  return toast.error(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: reportable ? (
      <ReportButton error={opts?.error} requestId={opts?.requestId} category={opts?.category} />
    ) : undefined,
  });
}

/**
 * Convenience for catch blocks: derives the message + request ID from an
 * ApiError (or any Error) and shows a reportable error toast. The most common
 * shape in the migrated call sites.
 */
export function notifyApiError(err: unknown, fallback = "Something went wrong", opts?: NotifyOptions): string | number {
  if (err instanceof ApiError) {
    return notifyError(err.message || fallback, {
      ...opts,
      requestId: err.requestId,
      error: { message: err.message },
    });
  }
  if (err instanceof Error) {
    return notifyError(err.message || fallback, {
      ...opts,
      error: { message: err.message, stack: err.stack },
    });
  }
  return notifyError(fallback, opts);
}
