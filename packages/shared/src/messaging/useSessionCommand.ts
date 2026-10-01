import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import { canSendToSession } from "./localSessionLabels";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt, LocalSessionEvent, SessionSend } from "./types";
import type { SessionControlClient } from "./sessionControls";
import { accessLost, type SessionSource } from "./useLocalSessionHistory";

export type SessionCommandAttempt = {
  request: LocalSessionCommand;
  state: "sending" | "uncertain" | "rejected" | "received";
  receipt?: LocalSessionCommandReceipt;
  error?: string;
};

/** Canonical command JSON is bounded to 8 KiB UTF-8, including every field. */
export const COMMAND_MAX_BYTES = 8192;

// Held room mentions can finish after native admission, so their reply check
// gets a separate bounded window; ordinary command checks use deliverBy.
export const AUTO_CHECK_INTERVAL_MS = 2000;
const HELD_REPLY_CHECK_LIMIT = 60;
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
  const chip: SessionReceiptChip = attempt.state === "sending" ? { label: "Sending", tone: "pending", detail: null }
    : attempt.state === "uncertain" ? { label: "Delivery unknown", tone: "warn", detail: attempt.error ?? UNKNOWN_DETAIL }
      : !receipt ? { label: "Not delivered", tone: "error", detail: attempt.error ?? null }
        : receipt.state === "unknown" ? { label: "Delivery unknown", tone: "warn", detail: attempt.error ?? UNKNOWN_DETAIL }
          : receipt.cancelledAt ? { label: "Cancelled", tone: "warn", detail: "This follow-up will not be delivered." }
            : attempt.request.kind === "send" && receipt.state === "applied" && receipt.replyPosted ? { label: "Delivered", tone: "ok", detail: "The session replied in this room." }
            : receipt.held ? { label: "Queued on your Mac", tone: "pending", detail: "Admission is not completed work." }
            : receipt.state === "applied" ? { label: attempt.request.kind === "interrupt" ? "Stop requested" : "Delivered", tone: "ok", detail: "Admission is not completed work." }
              : receipt.state === "rejected" ? { label: "Not delivered", tone: "error", detail: "This follow-up was not delivered." }
                : receipt.state === "pending" && receipt.deliverBy - receipt.createdAt > AUTO_CHECK_WINDOW_SECONDS
                  ? { label: "Queued for session", tone: "pending", detail: "The Mac will recheck this session before delivery. Expires after 24 hours." }
                  : { label: "Sending", tone: "pending", detail: receipt.state === "dispatched" ? "Waiting for the session to accept it." : null };
  return receipt?.actor ? { ...chip, label: `${agentName(receipt.actor.id)} · ${chip.label}` } : chip;
}

export function useSessionSendReceipt(
  getSessionCommand: ((sessionId: string, requestId: string) => Promise<LocalSessionCommandReceipt>) | undefined,
  send: SessionSend,
  refreshKey?: number,
): { chip: SessionReceiptChip | null } {
  const [receipt, setReceipt] = useState<LocalSessionCommandReceipt | null>(null);
  const [failed, setFailed] = useState(false);
  const heldReplyChecks = useRef<{ key: string; refreshKey?: number; count: number } | null>(null);
  useEffect(() => {
    if (!getSessionCommand || !send.requestId || send.error) return;
    const key = `${send.sessionId}/${send.requestId}`;
    if (heldReplyChecks.current?.key !== key || heldReplyChecks.current.refreshKey !== refreshKey) {
      heldReplyChecks.current = { key, refreshKey, count: 0 };
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async (count: number) => {
      try {
        const next = await getSessionCommand(send.sessionId, send.requestId!);
        if (cancelled) return;
        setReceipt(next);
        setFailed(false);
        const heldRoomSend = next.kind === "send" && next.state === "applied" && next.held && !next.replyPosted;
        const heldCount = heldRoomSend ? ++heldReplyChecks.current!.count : 0;
        const unsettled = next.state === "pending" || next.state === "dispatched" || next.state === "unknown" || heldRoomSend;
        const limit = Math.max(1, Math.ceil((next.deliverBy - next.createdAt) / (AUTO_CHECK_INTERVAL_MS / 1000)));
        if (heldRoomSend ? heldCount < HELD_REPLY_CHECK_LIMIT : unsettled && count < limit) timer = setTimeout(() => void poll(count + 1), AUTO_CHECK_INTERVAL_MS);
      } catch {
        if (cancelled) return;
        setFailed(true);
        if (count < 5) timer = setTimeout(() => void poll(count + 1), AUTO_CHECK_INTERVAL_MS);
      }
    };
    void poll(0);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [getSessionCommand, send.sessionId, send.requestId, send.error, refreshKey]);
  if (send.error) return { chip: { label: "Not delivered", tone: "error", detail: send.error } };
  if (!send.requestId || !getSessionCommand) return { chip: null };
  if (!receipt || receipt.requestId !== send.requestId) return { chip: failed
    ? { label: "Delivery unknown", tone: "warn", detail: UNKNOWN_DETAIL }
    : { label: "Sending", tone: "pending", detail: null } };
  return { chip: receiptChip({
    request: { requestId: send.requestId, expectedGeneration: receipt.expectedGeneration, kind: "send", text: "" },
    state: "received", receipt,
  }) };
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
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const refresh = () => client.listLocalSessionOutbox?.(sessionId)
    .then(page => { if (alive.current) setSnapshot({ sessionId, commands: page.commands }); })
    .catch(() => {});
  const cancel = async (requestId: string) => {
    try {
      const receipt = await client.cancelLocalSessionCommand?.(sessionId, requestId);
      if (receipt && alive.current) setSnapshot(current => ({ sessionId,
        commands: current.commands.map(command => command.requestId === requestId ? receipt : command) }));
    } catch { await refresh(); }
  };
  useEffect(() => {
    let active = true;
    void client.listLocalSessionOutbox?.(sessionId)
      .then(page => { if (active) setSnapshot({ sessionId, commands: page.commands }); })
      .catch(() => {});
    return () => { active = false; };
  }, [client, sessionId, revision]);
  const nextExpiry = Math.min(...commands.filter(command => command.state === "pending").map(command => command.deliverBy));
  useEffect(() => {
    if (!Number.isFinite(nextExpiry) || !client.listLocalSessionOutbox) return;
    const timer = setTimeout(() => { void client.listLocalSessionOutbox?.(sessionId)
      .then(page => setSnapshot({ sessionId, commands: page.commands })).catch(() => {}); },
    Math.max(0, nextExpiry * 1000 - Date.now()) + 1000);
    return () => clearTimeout(timer);
  }, [client, sessionId, nextExpiry]);
  const unsettled = commands.some(command => command.state === "pending" || command.state === "dispatched" || command.state === "unknown");
  useEffect(() => {
    if (!online || !unsettled || !client.listLocalSessionOutbox) return;
    let active = true;
    let count = 0;
    const timer = setInterval(() => {
      if (++count >= 3) clearInterval(timer);
      void client.listLocalSessionOutbox?.(sessionId)
        .then(page => { if (active) setSnapshot({ sessionId, commands: page.commands }); })
        .catch(() => {});
    }, 10_000);
    return () => { active = false; clearInterval(timer); };
  }, [client, sessionId, online, unsettled]);
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
  { uuid, onAccessLost, onSent, agentName, allowInterrupt = false }: { uuid: () => string; onAccessLost: () => void; onSent?: () => void; agentName?: (id: string) => string; allowInterrupt?: boolean },
) {
  const [attempt, setAttempt] = useState<SessionCommandAttempt | null>(null);
  const [checking, setChecking] = useState(false);
  const [autoCheckExhaustedFor, setAutoCheckExhaustedFor] = useState<string | null>(null);
  const alive = useRef(true);
  const pending = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const canSend = canSendToSession(session);
  const active = session.status === "busy" && !!session.activeTurnId && canSend;
  const canSteer = active && session.capabilities.steer;
  const canInterrupt = active && session.capabilities.interrupt && allowInterrupt;
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

  async function submit(request: LocalSessionCommand) {
    if (pending.current || !canSend) return;
    pending.current = true;
    setAttempt({ request, state: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(session.sessionId, request);
      if (!alive.current) return;
      setAttempt({ request, state: "received", receipt });
      if (request.kind === "send") onSent?.();
    } catch (e) {
      if (!alive.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      const rejected = e instanceof ApiError && e.status < 500;
      const code = e instanceof ApiError && e.body && typeof e.body === "object" && "code" in e.body ? e.body.code : null;
      setAttempt({ request, state: rejected ? "rejected" : "uncertain", error: rejected
        ? code === "LOCAL_SESSION_QUEUE_FULL" ? "This session already has 20 waiting follow-ups. Try again after one is delivered."
          : e.status === 409 ? "Session changed or is offline. Refresh before sending again."
          : "The session rejected this request. Check its availability and capabilities."
        : "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    } finally { pending.current = false; }
  }

  function send(text: string, now = false) {
    const trimmed = text.trim();
    if (locked || !trimmed || !canSend || (now && !canSteer)) return;
    const request: LocalSessionCommand = { requestId: uuid(), expectedGeneration: session.generation, kind: "send", text: trimmed,
      ...((now || (session.status === "busy" && session.capabilities.sendMode === "immediate")) ? { expectedTurnId: session.activeTurnId } : {}) };
    if (new TextEncoder().encode(JSON.stringify({ ...request, expectedTurnId: request.expectedTurnId ?? null })).length > COMMAND_MAX_BYTES) {
      setAttempt({ request, state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
      return;
    }
    void submit(request);
  }

  function interrupt() {
    if (locked || !canInterrupt || !session.activeTurnId) return;
    void submit({ requestId: uuid(), expectedGeneration: session.generation, expectedTurnId: session.activeTurnId, kind: "interrupt" });
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
  const canCancel = !!receipt && receipt.state === "pending" && !receipt.cancelledAt &&
    receipt.deliverBy - receipt.createdAt > AUTO_CHECK_WINDOW_SECONDS && !checking;
  async function cancel() {
    if (!canCancel || !requestId) return;
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
  return { attempt, receipt, chip: receiptChip(attempt, agentName), canSend, canSteer, canInterrupt, canCancel, locked, checking, canCheck, send, steer: (text: string) => send(text, true), interrupt, cancel, retry, checkDelivery };
}
