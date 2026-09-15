import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import { canSendToSession } from "./localSessionLabels";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import { accessLost, type RoomResourceSource } from "./useRoomResources";

export type SessionCommandAttempt = {
  request: LocalSessionCommand;
  state: "sending" | "uncertain" | "rejected" | "received";
  receipt?: LocalSessionCommandReceipt;
  error?: string;
};

/** Canonical command JSON is bounded to 8 KiB UTF-8, including every field. */
export const COMMAND_MAX_BYTES = 8192;

// The server's own delivery window (deliverBy - createdAt) is 30s in the fixtures;
// 15 checks * 2s covers it without a caller ever having to click "Check delivery".
const AUTO_CHECK_INTERVAL_MS = 2000;
const AUTO_CHECK_LIMIT = 15;

/** The receipt line both composers show; null when there is nothing to say. */
export function receiptCopy(receipt: LocalSessionCommandReceipt | undefined): string | null {
  if (!receipt) return null;
  const text = receipt.state === "unknown"
    ? "Delivery unknown. The request may have been admitted. Further remote input remains paused until this is resolved or the native session restarts. A restart does not undo an admitted command; do not send a duplicate."
    : receipt.held ? "Queued or held by the native session."
      : receipt.state === "applied" ? "Admitted to the native session — not completed work."
        : receipt.state === "pending" ? "Pending delivery to the desktop."
          : receipt.state === "dispatched" ? "Dispatched; native admission is not yet confirmed."
            : receipt.state === "rejected" ? "The desktop rejected this request." : null;
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
  client: RoomResourceSource["client"], roomId: string, session: LocalSession,
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

  // Auto-settle a pending/dispatched receipt in the background so "Check delivery"
  // only ever appears once delivery is genuinely stuck (owner report: it sat there
  // needing a click for every follow-up). Never touches `checking` or `onSent`.
  const requestId = attempt?.request.requestId;
  const receiptPolling = attempt?.receipt?.state === "pending" || attempt?.receipt?.state === "dispatched";
  useEffect(() => {
    if (!requestId || !receiptPolling) return;
    let cancelled = false;
    let count = 0;
    let timer: ReturnType<typeof setTimeout>;
    const tick = () => {
      timer = setTimeout(async () => {
        if (cancelled) return;
        count += 1;
        try {
          const receipt = await client.getLocalSessionCommand(roomId, session.sessionId, requestId);
          if (cancelled || !alive.current) return;
          setAttempt(current => current && current.request.requestId === requestId
            ? { ...current, state: "received", receipt, error: undefined } : current);
          if (receipt.state !== "pending" && receipt.state !== "dispatched") return; // settled; stop
        } catch (e) {
          if (cancelled || !alive.current) return;
          if (e instanceof ApiError && e.status === 403) { onAccessLost(); return; }
          // network / 5xx: swallow and just wait for the next tick
        }
        if (cancelled) return;
        if (count < AUTO_CHECK_LIMIT) tick(); else setAutoCheckExhaustedFor(requestId);
      }, AUTO_CHECK_INTERVAL_MS);
    };
    tick();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [requestId, receiptPolling, client, roomId, session.sessionId, onAccessLost]);

  async function submit(request: LocalSessionCommand) {
    if (pending.current || !canSend) return;
    pending.current = true;
    setAttempt({ request, state: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(roomId, session.sessionId, request);
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
      const receipt = await client.getLocalSessionCommand(roomId, session.sessionId, attempt.request.requestId);
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
