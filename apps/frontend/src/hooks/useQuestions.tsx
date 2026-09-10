// frontend/src/hooks/useQuestions.tsx
/**
 * Structured-question queue (OpenClaw's `ask_user` / `secrets` tools).
 *
 * WHY THIS EXISTS: `ask_user` is not in our tool deny list, and when an agent
 * calls it the run BLOCKS in-tool for up to 900s
 * (DEFAULT_ASK_USER_TIMEOUT_SECONDS) waiting for an operator. Until this hook
 * shipped nothing in the web UI could answer, so the tool timed out and
 * returned `no_answer` — the agent then proceeded on a guess. Answering is the
 * whole point: Friday asks instead of guessing.
 *
 * Contract (upstream packages/gateway-protocol/src/schema/questions.ts):
 *   event  question.requested  -> QuestionRecord (1–3 questions)
 *   event  question.resolved   -> {id, status: answered|cancelled|expired}
 *   rpc    question.list  {}   -> {questions: QuestionRecord[]}  (reload recovery)
 *   rpc    question.resolve    -> {id, answers:{answers:{[qid]: string[]}},
 *                                  secretStoreAllowedHosts?} | {id, cancel:true}
 * Both events ride the backend's generic forward branch (connection_pool.py)
 * and both RPCs pass through the `req` proxy untouched — only the four
 * run-dispatch methods are refused.
 *
 * Shaped after useExecApprovals, minus its clock-skew auto-prune machinery:
 * the gateway is the authority on expiry and emits `question.resolved`
 * with status "expired" itself, so there is nothing for a local timer to do
 * that wouldn't reintroduce the browser-vs-container skew bug. A card the
 * server has forgotten (container restart) is dropped on the resolve error.
 */

"use client";

import * as React from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@clerk/nextjs";
import type { QuestionRecord, RuntimeQuestionResolveRequest } from "@lightbulb/shared";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { isSessionKeyForUser } from "@/lib/sessionKey";

export type QuestionAnswerValues = Record<string, string[]>;

export interface UseQuestionsReturn {
  /** Pending requests for this operator's own web chat, in arrival order. */
  questions: QuestionRecord[];
  /** Submit answers. `allowedHosts` only for a secret-store question. */
  answer: (
    id: string,
    answers: QuestionAnswerValues,
    allowedHosts?: string[],
  ) => Promise<void>;
  /** Decline to answer — the tool returns `no_answer` and the run continues. */
  skip: (id: string) => Promise<void>;
  /** Last resolve error (cleared on the next attempt). */
  error: string | null;
}

/** Display-only fallback when a record arrives without a usable expiry. */
const DEFAULT_EXPIRY_MS = 10 * 60 * 1000;

/** Structural check — the payload crosses a WS boundary untyped. */
function parseRecord(payload: unknown): QuestionRecord | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as Partial<QuestionRecord>;
  if (typeof p.id !== "string" || !p.id) return null;
  if (!Array.isArray(p.questions) || p.questions.length === 0) return null;
  if (!p.questions.every((q) => q && typeof q.questionId === "string")) return null;
  if (p.status !== "pending") return null; // resolved records are not actionable
  // The gateway owns expiry, so a missing/malformed timestamp must not kill
  // the card -- only the countdown, which would otherwise render "NaN:NaN".
  const expiresAtMs = Number.isFinite(p.expiresAtMs) ? (p.expiresAtMs as number) : Date.now() + DEFAULT_EXPIRY_MS;
  return { ...p, expiresAtMs } as QuestionRecord;
}

export function useQuestions(): UseQuestionsReturn {
  const { onEvent, runtime, connectionRevision } = useLiveEvents();
  const { userId } = useAuth();
  const userIdRef = useRef(userId);
  useEffect(() => {
    userIdRef.current = userId;
  }, [userId]);

  const [questions, setQuestions] = useState<QuestionRecord[]>([]);
  const [error, setError] = useState<string | null>(null);

  const enqueue = useCallback((records: QuestionRecord[]) => {
    if (records.length === 0) return;
    setQuestions((prev) => {
      const seen = new Set(prev.map((q) => q.id));
      const fresh = records.filter((r) => !seen.has(r.id));
      return fresh.length === 0 ? prev : [...prev, ...fresh];
    });
  }, []);

  const remove = useCallback((id: string) => {
    setQuestions((prev) => prev.filter((q) => q.id !== id));
  }, []);

  // question.requested / question.resolved.
  useEffect(() => {
    return onEvent((event) => {
      if (event.type === "runtime.question.requested") {
        const record = parseRecord(event.payload);
        if (!record) return;
        // Multi-tenancy: the backend broadcasts this event to every frontend
        // of the owner, so scope it to this member's own web chat here.
        // Paperclip-session questions are dropped — see the TODO below.
        if (!isSessionKeyForUser(record.sessionKey, userIdRef.current)) return;
        enqueue([record]);
        return;
      }
      if (event.type === "runtime.question.resolved") {
        remove(event.payload.id);
      }
    });
  }, [onEvent, enqueue, remove]);

  // Reload on mount and after each AppSync reconnect. The runtime stays
  // authoritative for questions that arrived while this client was offline.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const outcome = await runtime.rpc<{ questions?: unknown[] }>("question.list", {});
        if (cancelled || outcome.kind !== "result" || !Array.isArray(outcome.value?.questions)) return;
        enqueue(
          outcome.value.questions
            .map(parseRecord)
            .filter((record): record is QuestionRecord => record !== null)
            .filter((record) => isSessionKeyForUser(record.sessionKey, userIdRef.current)),
        );
      } catch {
        // A sleeping/unavailable runtime is normal; the next reconnect retries.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [connectionRevision, runtime, enqueue]);

  const resolve = useCallback(
    async (id: string, params: RuntimeQuestionResolveRequest): Promise<void> => {
      setError(null);
      try {
        await runtime.resolveQuestion(id, params);
        remove(id);
      } catch (e) {
        const msg = e instanceof Error ? e.message : "Failed to send answer";
        // The question manager is in-memory in the per-user container: after a
        // restart the id is gone, and a question answered in another tab (or
        // expired server-side before its `question.resolved` reached us) is
        // already terminal. Both are dead cards — every retry reproduces the
        // same error — so drop them instead of wedging the queue. The backend
        // JSON-serializes the whole gateway error object into the message, so
        // the manager's own reason code is matchable (QuestionManagerErrorCodes
        // in upstream src/gateway/question-manager.ts). QUESTION_INVALID_ANSWER
        // is deliberately NOT here: that one the operator can fix and retry.
        if (/QUESTION_NOT_FOUND|QUESTION_ALREADY_TERMINAL/.test(msg)) {
          remove(id);
          return;
        }
        setError(msg);
        throw e;
      }
    },
    [runtime, remove],
  );

  const answer = useCallback(
    (id: string, answers: QuestionAnswerValues, allowedHosts?: string[]) =>
      resolve(id, {
        // Double nesting is the wire schema, not a typo: QuestionResolveParams
        // takes a QuestionAnswers object, which itself wraps `answers`.
        answers: { answers },
        // Sending this for a non-secret question is a hard INVALID_REQUEST.
        ...(allowedHosts ? { secretStoreAllowedHosts: allowedHosts } : {}),
      }),
    [resolve],
  );

  const skip = useCallback((id: string) => resolve(id, { cancel: true }), [resolve]);

  return { questions, answer, skip, error };
}

// =============================================================================
// React Context — one subscription per app shell.
// =============================================================================

const QuestionsContext = React.createContext<UseQuestionsReturn | null>(null);

export function QuestionsProvider({ children }: { children: React.ReactNode }) {
  const value = useQuestions();
  return <QuestionsContext.Provider value={value}>{children}</QuestionsContext.Provider>;
}

/** Stable empty default so consumers outside the provider (tests, surfaces
 *  that don't mount it) don't refire effects on every parent render. */
const EMPTY_QUESTIONS_CONTEXT: UseQuestionsReturn = {
  questions: [],
  answer: async () => {},
  skip: async () => {},
  error: null,
};

export function useQuestionsContext(): UseQuestionsReturn {
  return React.useContext(QuestionsContext) ?? EMPTY_QUESTIONS_CONTEXT;
}
