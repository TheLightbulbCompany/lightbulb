import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import { canSendToSession, sessionEndable } from "./localSessionLabels";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt, LocalSessionEvent } from "./types";
import type { SessionControlClient } from "./sessionControls";
import { accessLost, type SessionSource } from "./useLocalSessionHistory";

export type SessionCommandAttempt = {
  request: LocalSessionCommand;
  replacesRequestId?: string;
  state: "sending" | "uncertain" | "rejected" | "received";
  receipt?: LocalSessionCommandReceipt;
  error?: string;
};

/** Canonical command JSON is bounded to 8 KiB UTF-8, including every field. */
export const COMMAND_MAX_BYTES = 8192;

export const AUTO_CHECK_INTERVAL_MS = 2000;
const AUTO_CHECK_WINDOW_SECONDS = 30;

/** The one status chip a pending follow-up carries; `detail` is the longer explanation (tooltip / second line). */
export type SessionReceiptChip = { label: string; tone: "pending" | "ok" | "warn" | "error"; detail: string | null };

const UNKNOWN_DETAIL = "It may already be on your Mac. Check delivery; do not send it again.";

/**
 * Presentation only: maps the attempt/receipt state machine onto one short
 * chip (Sending · Queued on your Mac · Delivered · Not delivered · Delivery
 * unknown). `agentName` resolves an agent-invoked receipt's actor id.
 */
export function receiptChip(attempt: SessionCommandAttempt | null | undefined, agentName: (id: string) => string = (id) => id): SessionReceiptChip | null {
  if (!attempt) return null;
  const receipt = attempt.receipt;
  const chip: SessionReceiptChip | null = attempt.state === "sending" ? { label: "Sending", tone: "pending", detail: null }
    : attempt.state === "uncertain" ? { label: "Delivery unknown", tone: "warn", detail: attempt.error ?? UNKNOWN_DETAIL }
      : !receipt ? { label: "Not delivered", tone: "error", detail: attempt.error ?? null }
        : receipt.state === "unknown" ? { label: "Delivery unknown", tone: "warn", detail: attempt.error ?? UNKNOWN_DETAIL }
          : receipt.cancelledAt ? { label: "Cancelled", tone: "warn", detail: "This follow-up will not be delivered." }
            : receipt.state === "rejected" ? attempt.request.kind === "end" ? { label: "Not ended", tone: "error", detail: "This session could not be ended from here. End it on its Mac." }
              : { label: "Not delivered", tone: "error", detail: "This follow-up was not delivered." }
            : receipt.state === "awaiting_approval" ? { label: "Waiting for the owner", tone: "pending", detail: "The session's owner decides whether it is delivered." }
            : attempt.request.kind === "send" && receipt.state === "applied" ? null
            : receipt.held ? { label: "Queued on your Mac", tone: "pending", detail: "Admission is not completed work." }
            : receipt.state === "applied" ? attempt.request.kind === "end" ? { label: "Ending", tone: "ok", detail: null }
              : { label: attempt.request.kind === "interrupt" ? "Stop requested" : "Delivered", tone: "ok", detail: "Admission is not completed work." }
                : receipt.state === "pending" && receipt.deliverBy - receipt.createdAt > AUTO_CHECK_WINDOW_SECONDS
                  ? { label: "Queued for session", tone: "pending", detail: "The Mac will recheck this session before delivery. Expires after 24 hours." }
                  : { label: "Sending", tone: "pending", detail: receipt.state === "dispatched" ? "Waiting for the session to accept it." : null };
  return chip && receipt?.actor ? { ...chip, label: `${agentName(receipt.actor.id)} · ${chip.label}` } : chip;
}

/**
 * The follow-up to draw as a greyed prompt row at the end of the transcript,
 * until the harness writes it back as a real prompt event. A pre-receipt
 * rejection (too long, stale generation) keeps its text in the composer, so it
 * gets no row. `baselineSeq` is the newest event seq when the attempt began,
 * so an older identical prompt never hides the new one.
 */
export function pendingPrompt(attempt: SessionCommandAttempt | null | undefined, events: readonly LocalSessionEvent[], baselineSeq: number): string | null {
  const text = attempt?.request.kind === "send" ? attempt.request.text : null;
  if (!attempt || !text || (attempt.state === "rejected" && !attempt.receipt)) return null;
  const echoed = events.some((e) => e.seq > baselineSeq && e.kind === "text" && e.role === "user" && e.text.trim() === text);
  return echoed ? null : text;
}

/** `pendingPrompt` with the baseline captured the first render each request is seen. */
export function usePendingPrompt(attempt: SessionCommandAttempt | null | undefined, events: readonly LocalSessionEvent[]): string | null {
  const baseline = useRef<{ requestId: string; seq: number } | null>(null);
  const requestId = attempt?.request.requestId;
  if (requestId && baseline.current?.requestId !== requestId) baseline.current = { requestId, seq: events.at(-1)?.seq ?? 0 };
  return pendingPrompt(attempt, events, baseline.current?.seq ?? 0);
}

/** Sender-private receipts survive phone restarts and are readable on another device. */
export function useSessionOutbox(client: SessionControlClient, sessionId: string, revision: unknown, online: boolean) {
  const [snapshot, setSnapshot] = useState<{ sessionId: string; commands: LocalSessionCommandReceipt[] }>({ sessionId, commands: [] });
  const commands = snapshot.sessionId === sessionId ? snapshot.commands : [];
  const alive = useRef(true);
  const requested = useRef(0);
  const applied = useRef(0);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const refresh = useCallback(async () => {
    if (!client.listLocalSessionOutbox) return;
    const version = ++requested.current;
    try {
      const page = await client.listLocalSessionOutbox(sessionId);
      if (alive.current && version >= applied.current) {
        applied.current = version;
        setSnapshot({ sessionId, commands: page.commands });
      }
    } catch { /* The next hint or poll retries. */ }
  }, [client, sessionId]);
  const cancel = async (requestId: string) => {
    try {
      const receipt = await client.cancelLocalSessionCommand?.(sessionId, requestId);
      if (receipt && alive.current) {
        applied.current = ++requested.current;
        setSnapshot(current => ({ sessionId,
          commands: current.sessionId === sessionId
            ? current.commands.map(command => command.requestId === requestId ? receipt : command)
            : [receipt] }));
      }
    } catch { await refresh(); }
  };
  useEffect(() => {
    void refresh();
  }, [refresh, revision]);
  const nextExpiry = Math.min(...commands.filter(command => command.state === "pending").map(command => command.deliverBy));
  useEffect(() => {
    if (!Number.isFinite(nextExpiry)) return;
    const timer = setTimeout(() => { void refresh(); }, Math.max(0, nextExpiry * 1000 - Date.now()) + 1000);
    return () => clearTimeout(timer);
  }, [nextExpiry, refresh]);
  const unsettled = commands.some(command => command.state === "awaiting_approval" || command.state === "pending" || command.state === "dispatched" || command.state === "unknown");
  const needsRefresh = unsettled || commands.some(command => command.state === "rejected");
  useEffect(() => {
    if (!client.listLocalSessionOutbox || (online && !needsRefresh)) return;
    const timer = setInterval(() => { void refresh(); }, 30_000);
    return () => clearInterval(timer);
  }, [client, online, needsRefresh, refresh]);
  return { commands, unsettled, refresh, cancel, canCancel: !!client.cancelLocalSessionCommand };
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
  { uuid, onAccessLost, onSent, agentName, allowInterrupt = false, allowOffline = false, allowEnd = false }: { uuid: () => string; onAccessLost: () => void; onSent?: () => void; agentName?: (id: string) => string; allowInterrupt?: boolean; allowOffline?: boolean; allowEnd?: boolean },
) {
  const [attempt, setAttempt] = useState<SessionCommandAttempt | null>(null);
  const [checking, setChecking] = useState(false);
  const [autoCheckExhaustedFor, setAutoCheckExhaustedFor] = useState<string | null>(null);
  const alive = useRef(true);
  const pending = useRef(false);
  const attemptRef = useRef(attempt);
  const onSentRef = useRef(onSent);
  attemptRef.current = attempt;
  onSentRef.current = onSent;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const canSend = canSendToSession(session, allowOffline);
  const active = session.status === "busy" && !!session.activeTurnId && canSend;
  const canSteer = active && session.capabilities.steer;
  const canInterrupt = active && session.capabilities.interrupt && allowInterrupt;
  // The owner's alone, and only while the Mac mirrors it (`sessionEndable`).
  const canEnd = allowEnd && sessionEndable(session);
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
    // Stop polling an offline inbox after 30 seconds; the session's online
    // transition rearms this bounded check, and manual Check delivery remains.
    const limit = Math.max(1, Math.ceil(Math.min(AUTO_CHECK_WINDOW_SECONDS, receipt.deliverBy - receipt.createdAt) / (AUTO_CHECK_INTERVAL_MS / 1000)));
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
  }, [requestId, receiptPolling, client, session.sessionId, session.status]);

  const offlineExpiry = attempt?.receipt?.state === "pending" && attempt.receipt.deliverBy - attempt.receipt.createdAt > AUTO_CHECK_WINDOW_SECONDS
    ? attempt.receipt.deliverBy : null;
  useEffect(() => {
    if (!requestId || offlineExpiry === null) return;
    const timer = setTimeout(() => { void client.getLocalSessionCommand(session.sessionId, requestId)
      .then(next => setAttempt(current => current?.request.requestId === requestId
        ? { ...current, state: "received", receipt: next } : current)).catch(() => {}); },
    Math.max(0, offlineExpiry * 1000 - Date.now()) + 1000);
    return () => clearTimeout(timer);
  }, [client, session.sessionId, requestId, offlineExpiry]);

  async function submit(request: LocalSessionCommand, replacesRequestId?: string) {
    if (pending.current || (!canSend && request.kind !== "end")) return; // ending needs no send capability
    pending.current = true;
    setAttempt({ request, replacesRequestId, state: "sending" });
    try {
      const receipt = replacesRequestId
        ? await client.submitLocalSessionCommand(session.sessionId, request, replacesRequestId)
        : await client.submitLocalSessionCommand(session.sessionId, request);
      if (!alive.current) return;
      setAttempt({ request, replacesRequestId, state: "received", receipt });
      if (request.kind === "send") onSent?.();
    } catch (e) {
      if (!alive.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      const rejected = e instanceof ApiError && e.status < 500;
      const code = e instanceof ApiError && e.body && typeof e.body === "object" && "code" in e.body ? e.body.code : null;
      setAttempt({ request, replacesRequestId, state: rejected ? "rejected" : "uncertain", error: rejected
        ? code === "LOCAL_SESSION_QUEUE_FULL" ? "This session already has 20 waiting follow-ups. Try again after one is delivered."
          : e.status === 409 ? "Session changed or is offline. Refresh before sending again."
          : "The session rejected this request. Check its availability and capabilities."
        : "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    } finally { pending.current = false; }
  }

  function send(text: string, now = false, replacesRequestId?: string) {
    const trimmed = text.trim();
    if (locked || !trimmed || !canSend || (now && !canSteer)) return;
    const request: LocalSessionCommand = { requestId: uuid(), expectedGeneration: session.generation, kind: "send", text: trimmed,
      ...((now || (session.status === "busy" && session.capabilities.sendMode === "immediate")) ? { expectedTurnId: session.activeTurnId } : {}) };
    if (new TextEncoder().encode(JSON.stringify({ ...request, expectedTurnId: request.expectedTurnId ?? null })).length > COMMAND_MAX_BYTES) {
      setAttempt({ request, state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
      return;
    }
    void submit(request, replacesRequestId);
    return request.requestId;
  }

  function interrupt() {
    if (locked || !canInterrupt || !session.activeTurnId) return;
    void submit({ requestId: uuid(), expectedGeneration: session.generation, expectedTurnId: session.activeTurnId, kind: "interrupt" });
  }

  /** Ends the session on its Mac, as closing its terminal would. The caller confirms first. */
  function end() {
    if (locked || !canEnd) return;
    void submit({ requestId: uuid(), expectedGeneration: session.generation, kind: "end" });
  }

  function retry() {
    if (attempt?.state === "uncertain") void submit(attempt.request, attempt.replacesRequestId);
    else if (attempt?.request.kind === "send" && (attempt.state === "rejected" || attempt.receipt?.state === "rejected"))
      void submit({ ...attempt.request, requestId: uuid(), expectedGeneration: session.generation }, attempt.replacesRequestId);
  }

  function dismiss() { if (attempt?.state === "rejected" || attempt?.receipt?.state === "rejected") setAttempt(null); }

  async function checkDelivery() {
    if (!attempt || pending.current) return;
    pending.current = true;
    setChecking(true);
    try {
      const receipt = await client.getLocalSessionCommand(session.sessionId, attempt.request.requestId);
      if (alive.current) {
        setAttempt({ ...attempt, state: "received", receipt, error: undefined });
        if (attempt.state === "uncertain" && attempt.request.kind === "send") onSent?.();
      }
    } catch (e) {
      if (!alive.current) return;
      // A missing receipt can mean the original request never arrived. Do not resend implicitly.
      if (e instanceof ApiError && e.status === 403) { onAccessLost(); return; }
      setAttempt({ ...attempt, error: "Delivery is still unconfirmed. Checking does not resend the request." });
    } finally { pending.current = false; if (alive.current) setChecking(false); }
  }

  const receipt = attempt?.receipt;
  const syncReceipt = useCallback((next: LocalSessionCommandReceipt) => {
    const current = attemptRef.current;
    if (current?.request.requestId === next.requestId && current.state === "uncertain" && current.request.kind === "send") {
      onSentRef.current?.();
    }
    setAttempt(current => {
      if (!current || current.request.requestId !== next.requestId || current.receipt === next) return current;
      if (current.receipt && ["applied", "rejected"].includes(current.receipt.state) &&
          !["applied", "rejected"].includes(next.state)) return current;
      return { ...current, state: "received", receipt: next };
    });
  }, []);
  const canCancel = !!receipt && receipt.state === "pending" && !receipt.cancelledAt &&
    receipt.deliverBy - receipt.createdAt > AUTO_CHECK_WINDOW_SECONDS && !checking && !!client.cancelLocalSessionCommand;
  async function cancel() {
    if (!canCancel || !requestId || !client.cancelLocalSessionCommand) return;
    setChecking(true);
    try {
      const next = await client.cancelLocalSessionCommand(session.sessionId, requestId);
      if (alive.current) setAttempt(current => current?.request.requestId === requestId
        ? { ...current, state: "received", receipt: next } : current);
    } catch (e) {
      if (accessLost(e)) onAccessLost();
      else await checkDelivery();
    } finally { if (alive.current) setChecking(false); }
  }
  const canCheck = attempt?.state === "uncertain" || receipt?.state === "unknown"
    || (receiptPolling && autoCheckExhaustedFor === requestId);
  return { attempt, receipt, chip: receiptChip(attempt, agentName), canSend, canSteer, canInterrupt, canEnd, canCancel, locked, checking, canCheck, send, steer: (text: string) => send(text, true), interrupt, end, cancel, retry, dismiss, checkDelivery, syncReceipt };
}
