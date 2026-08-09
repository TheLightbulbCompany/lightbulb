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

export interface NotifyActionOptions extends NotifyOptions {
  /** Optional action button rendered on the toast's trailing edge. */
  action?: { label: string; onClick: () => void };
  /** Override the 4s auto-dismiss. Only worth setting when the action is the
   * user's only route back from something otherwise irreversible. */
  durationMs?: number;
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
export function notifySuccess(message: string, opts?: NotifyActionOptions): string | number {
  return toast.success(message, {
    id: opts?.dedupeKey,
    duration: opts?.durationMs ?? AUTO_DISMISS_MS,
    action: opts?.action,
  });
}

/**
 * Persistent progress toast (spinner, never auto-dismisses). Resolve it by
 * calling notifySuccess/notifyFailure/dismissNotification with the same
 * dedupeKey — sonner replaces the toast in place by id.
 */
export function notifyLoading(message: string, opts?: NotifyActionOptions): string | number {
  return toast.loading(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: opts?.action,
  });
}

/**
 * Sticky failure toast with a caller-supplied action. Distinct from
 * notifyError, which owns the support Report flow and its ReportButton.
 */
export function notifyFailure(message: string, opts?: NotifyActionOptions): string | number {
  return toast.error(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: opts?.action,
  });
}

/**
 * Sticky attention toast (info-styled, never auto-dismisses). For events that
 * wait on the user (e.g. an agent awaiting approval) — resolved by a
 * dismissNotification with the same dedupeKey when the wait ends.
 */
export function notifyAttention(message: string, opts?: NotifyActionOptions): string | number {
  return toast.info(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: opts?.action,
  });
}

export function dismissNotification(id: string | number): void {
  toast.dismiss(id);
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
 * Structured `{blocked: true, code, message}` detail — the shape every trial /
 * subscription gate 402 in the system uses (see routers/teams/_guards.py).
 */
function blockedGateDetail(body: unknown): { code?: string; message?: string } | null {
  const detail =
    body && typeof body === "object" && "detail" in body
      ? (body as { detail?: unknown }).detail
      : undefined;
  if (!detail || typeof detail !== "object") return null;
  const d = detail as { blocked?: unknown; code?: unknown; message?: unknown };
  if (d.blocked !== true) return null;
  return {
    code: typeof d.code === "string" ? d.code : undefined,
    message: typeof d.message === "string" ? d.message : undefined,
  };
}

/**
 * Convenience for catch blocks: derives the message + request ID from an
 * ApiError (or any Error) and shows a reportable error toast. The most common
 * shape in the migrated call sites.
 *
 * A blocked-gate 402 is the exception: hitting the free-tier cap is not a
 * malfunction, so it renders as an info-styled attention toast with an
 * Upgrade action instead of a red Report-able error. This is the single
 * conversion surface for every Teams action the trial guards refuse
 * (comments, approvals, check-now, retry) — the call sites stay dumb.
 */
export function notifyApiError(err: unknown, fallback = "Something went wrong", opts?: NotifyOptions): string | number {
  if (err instanceof ApiError) {
    const gate = blockedGateDetail(err.body);
    if (gate) {
      return notifyAttention(gate.message || err.message || fallback, {
        ...opts,
        // One toast per gate, however many blocked clicks — sonner replaces
        // by id, so a user retrying doesn't stack identical upgrade prompts.
        dedupeKey: opts?.dedupeKey ?? `gate:${gate.code ?? "blocked"}`,
        action: {
          label: "Upgrade",
          // notify.tsx is a plain module (no router hook available), and
          // /settings is a different Next route anyway — a hard navigation is
          // exactly what the cross-route <Link> in ChatErrorCard does too.
          onClick: () => window.location.assign("/settings?panel=billing"),
        },
      });
    }
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
