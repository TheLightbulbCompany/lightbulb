import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import { canSendToSession } from "./localSessionLabels";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import { accessLost, type SessionSource } from "./useLocalSessionHistory";

export type SessionCommandAttempt = {
  request: LocalSessionCommand;
  state: "sending" | "uncertain" | "rejected" | "received";
  receipt?: LocalSessionCommandReceipt;
  error?: string;
};

/** Canonical command JSON is bounded to 8 KiB UTF-8, including every field. */
export const COMMAND_MAX_BYTES = 8192;

// The auto-check interval. The per-request check budget is derived from the
// receipt's own delivery window (deliverBy - createdAt: 30s for a human sender,
// shorter for an agent invocation) so a caller never has to click "Check
// delivery" before the server itself would have moved the receipt on.
export const AUTO_CHECK_INTERVAL_MS = 2000;

/** The receipt line both composers show; null when there is nothing to say. */
export function receiptCopy(receipt: LocalSessionCommandReceipt | undefined): string | null {
  if (!receipt) return null;
  const text = receipt.state === "unknown"
    ? "Delivery unknown. The request may have been admitted. Further remote input remains paused until this is resolved or the native session restarts. A restart does not undo an admitted command; do not send a duplicate."
    : receipt.held ? "Queued on your Mac."
      : receipt.state === "applied" ? "Delivered."
        : receipt.state === "pending" ? "Sending to your Mac…"
          : receipt.state === "dispatched" ? "Sent to your Mac; waiting for the session to accept it…"
            : receipt.state === "rejected" ? "Not delivered: your Mac rejected this follow-up." : null;
  return text && receipt.actor ? `Agent ${receipt.actor.id}: ${text}` : text;
}

/**
 * One follow-up at a time into a shared native session: send, remember the
 * attempt, check delivery, retry the SAME request. Never resends implicitly —
 * an uncertain send may already have been admitted on the owner's Mac.
 * `uuid` is injected because the web has `crypto.randomUUID` and the phone
 * has expo-crypto. `onSent` fires when the composer should clear its draft.
 */
export function useSessionCommand(
  client: SessionSource["client"], session: LocalSession,
  { uuid, onAccessLost, onSent }: { uuid: () => string; onAccessLost: () => void; onSent?: () => void },
) {
  const [attempt, setAttempt] = useState<SessionCommandAttempt | null>(null);
  const [checking, setChecking] = useState(false);
  const [autoCheckExhaustedFor, setAutoCheckExhaustedFor] = useState<string | null>(null);
  const alive = useRef(true);
  const pending = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const canSend = canSendToSession(session);
  const unknownCurrent = attempt?.receipt?.state === "unknown" && attempt.receipt.expectedGeneration === session.generation;
  const locked = checking || attempt?.state === "sending" || attempt?.state === "uncertain" || unknownCurrent;

  // A future caller passing an inline onAccessLost would otherwise re-identity
  // every render, restarting the effect below and resetting its budget forever.
  const onAccessLostRef = useRef(onAccessLost);
  onAccessLostRef.current = onAccessLost;

  // Auto-settle a pending/dispatched receipt in the background so "Check delivery"
  // only ever appears once delivery is genuinely stuck (owner report: it sat there
  // needing a click for every follow-up). Never touches `checking` or `onSent`.
  const requestId = attempt?.request.requestId;
  // "unknown" too: the desktop settles an unknown admission on its next heartbeat (~10s), so
  // a delivered follow-up whose first receipt raced the transcript write still turns Delivered.
  const unsettled = (state: LocalSessionCommandReceipt["state"] | undefined) =>
    state === "pending" || state === "dispatched" || state === "unknown";
  const receiptPolling = unsettled(attempt?.receipt?.state);
  useEffect(() => {
    const receipt = attempt?.receipt;
    if (!requestId || !receiptPolling || !receipt) return;
    // The per-request budget: enough 2s checks to cover the server's own
    // delivery window for THIS receipt, never fewer than one.
    const limit = Math.max(1, Math.ceil((receipt.deliverBy - receipt.createdAt) / (AUTO_CHECK_INTERVAL_MS / 1000)));
    let cancelled = false;
    let count = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      timer = setTimeout(async () => {
        if (cancelled) return;
        count += 1;
        try {
          const next = await client.getLocalSessionCommand(session.sessionId, requestId);
          if (cancelled || !alive.current) return;
          setAttempt(current => current && current.request.requestId === requestId
            ? { ...current, state: "received", receipt: next, error: undefined } : current);
          if (!unsettled(next.state)) return; // settled; stop
        } catch (e) {
          if (cancelled || !alive.current) return;
          if (e instanceof ApiError && e.status === 403) { onAccessLostRef.current(); return; }
          // network / 5xx: swallow and just wait for the next tick
        }
        if (cancelled) return;
        if (count < limit) tick(); else setAutoCheckExhaustedFor(requestId);
      }, AUTO_CHECK_INTERVAL_MS);
    };
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [requestId, receiptPolling, client, session.sessionId]);

  async function submit(request: LocalSessionCommand) {
    if (pending.current || !canSend) return;
    pending.current = true;
    setAttempt({ request, state: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(session.sessionId, request);
      if (!alive.current) return;
      setAttempt({ request, state: "received", receipt });
      onSent?.();
    } catch (e) {
      if (!alive.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      const rejected = e instanceof ApiError && e.status < 500;
      setAttempt({ request, state: rejected ? "rejected" : "uncertain", error: rejected
        ? e.status === 409 ? "Session changed or is offline. Refresh before sending again."
          : "The session rejected this request. Check its availability and capabilities."
        : "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    } finally { pending.current = false; }
  }

  function send(text: string) {
    const trimmed = text.trim();
    if (locked || !trimmed || !canSend) return;
    const request: LocalSessionCommand = { requestId: uuid(), expectedGeneration: session.generation, kind: "send", text: trimmed,
      ...(session.status === "busy" && session.capabilities.sendMode === "immediate" ? { expectedTurnId: session.activeTurnId } : {}) };
    if (new TextEncoder().encode(JSON.stringify({ ...request, expectedTurnId: request.expectedTurnId ?? null })).length > COMMAND_MAX_BYTES) {
      setAttempt({ request, state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
      return;
    }
    void submit(request);
  }

  function retry() {
    if (attempt?.state === "uncertain") void submit(attempt.request);
  }

  async function checkDelivery() {
    if (!attempt || pending.current) return;
    pending.current = true;
    setChecking(true);
    try {
      const receipt = await client.getLocalSessionCommand(session.sessionId, attempt.request.requestId);
      if (alive.current) {
        setAttempt({ ...attempt, state: "received", receipt, error: undefined });
        if (attempt.state === "uncertain") onSent?.();
      }
    } catch (e) {
      if (!alive.current) return;
      // A missing receipt can mean the original request never arrived. Do not resend implicitly.
      if (e instanceof ApiError && e.status === 403) { onAccessLost(); return; }
      setAttempt({ ...attempt, error: "Delivery is still unconfirmed. Checking does not resend the request." });
    } finally { pending.current = false; if (alive.current) setChecking(false); }
  }

  const receipt = attempt?.receipt;
  const canCheck = attempt?.state === "uncertain" || receipt?.state === "unknown"
    || (receiptPolling && autoCheckExhaustedFor === requestId);
  return { attempt, receipt, receiptText: receiptCopy(receipt), canSend, locked, checking, canCheck, send, retry, checkDelivery };
}
