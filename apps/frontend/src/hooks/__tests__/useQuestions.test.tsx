import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { useQuestions } from "../useQuestions";
import type { LiveEvent, QuestionRecord } from "@lightbulb/shared";

type EventHandler = (event: LiveEvent) => void;
const eventHandlers: Set<EventHandler> = new Set();
let sendReqMock: ReturnType<typeof vi.fn> = vi.fn();
let connectionRevision = 1;
const runtime = {
  rpc: (...args: unknown[]) => sendReqMock(...args).then((value: unknown) => ({ kind: "result", value })),
  resolveQuestion: (id: string, params: Record<string, unknown>) =>
    sendReqMock("question.resolve", { id, ...params }),
};

vi.mock("@/hooks/useLiveEvents", () => ({
  useLiveEvents: () => ({
    connectionRevision,
    runtime,
    onEvent: (handler: EventHandler) => {
      eventHandlers.add(handler);
      return () => eventHandlers.delete(handler);
    },
  }),
}));

vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ userId: "user_test" }),
}));

function record(over: Partial<QuestionRecord> = {}): QuestionRecord {
  return {
    id: "q_req_1",
    questions: [
      {
        questionId: "deploy_target",
        header: "Deploy",
        question: "Where should I deploy?",
        options: [{ label: "Vercel" }, { label: "Fly.io" }],
      },
    ],
    agentId: "agent_x",
    sessionKey: "agent:agent_x:main",
    createdAtMs: Date.now(),
    expiresAtMs: Date.now() + 900_000,
    status: "pending",
    ...over,
  };
}

function emit(event: "question.requested" | "question.resolved", payload: unknown) {
  const liveEvent = {
    schemaVersion: 1,
    eventId: `event-${event}`,
    type: event === "question.requested"
      ? "runtime.question.requested"
      : "runtime.question.resolved",
    occurredAt: "2026-09-09T00:00:00.000Z",
    payload,
  } as LiveEvent;
  for (const handler of eventHandlers) handler(liveEvent);
}

/** The reload-recovery effect fires question.list on every mount, so tests
 *  must stub per METHOD — a bare mockRejectedValueOnce would be eaten by it. */
function resolveRejects(error: Error) {
  sendReqMock.mockImplementation((method: string) =>
    method === "question.resolve"
      ? Promise.reject(error)
      : Promise.resolve({ questions: [] }),
  );
}

describe("useQuestions", () => {
  beforeEach(() => {
    eventHandlers.clear();
    connectionRevision = 1;
    // Default: question.list returns nothing so the seeding effect is inert.
    sendReqMock = vi.fn().mockResolvedValue({ questions: [] });
  });

  it("enqueues a pending question from question.requested", async () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));
    expect(result.current.questions).toHaveLength(1);
    expect(result.current.questions[0].questions[0].question).toBe(
      "Where should I deploy?",
    );
  });

  it("is idempotent — a redelivered request does not duplicate", () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));
    act(() => emit("question.requested", record()));
    expect(result.current.questions).toHaveLength(1);
  });

  it("drops another org member's question", () => {
    const { result } = renderHook(() => useQuestions());
    act(() =>
      emit("question.requested", record({ sessionKey: "agent:agent_x:user_other" })),
    );
    expect(result.current.questions).toHaveLength(0);
  });

  it("drops Paperclip-session questions (Teams surface owns those)", () => {
    const { result } = renderHook(() => useQuestions());
    act(() =>
      emit(
        "question.requested",
        record({ sessionKey: "agent:agent_x:paperclip:issue:i9" }),
      ),
    );
    expect(result.current.questions).toHaveLength(0);
  });

  it("ignores a non-pending record", () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record({ status: "expired" })));
    expect(result.current.questions).toHaveLength(0);
  });

  it("keeps a record whose expiry is missing, with a finite fallback countdown", () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record({ expiresAtMs: undefined as unknown as number })));
    expect(result.current.questions).toHaveLength(1);
    expect(Number.isFinite(result.current.questions[0].expiresAtMs)).toBe(true);
    expect(result.current.questions[0].expiresAtMs).toBeGreaterThan(Date.now());
  });

  it("question.resolved clears the card (answered elsewhere / expired)", () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));
    act(() => emit("question.resolved", { id: "q_req_1", status: "expired" }));
    expect(result.current.questions).toHaveLength(0);
  });

  it("answer() sends the schema-shaped question.resolve and drops the card", async () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await result.current.answer("q_req_1", { deploy_target: ["Vercel"] });
    });

    expect(sendReqMock).toHaveBeenCalledWith("question.resolve", {
      id: "q_req_1",
      answers: { answers: { deploy_target: ["Vercel"] } },
    });
    expect(result.current.questions).toHaveLength(0);
  });

  it("skip() sends cancel:true", async () => {
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await result.current.skip("q_req_1");
    });

    expect(sendReqMock).toHaveBeenCalledWith("question.resolve", {
      id: "q_req_1",
      cancel: true,
    });
    expect(result.current.questions).toHaveLength(0);
  });

  it("drops the card when the server no longer knows the id", async () => {
    // Container restart wipes the in-memory question manager; retrying the
    // same id can only ever produce the same error. The backend serializes the
    // whole gateway error object into the message.
    resolveRejects(
      new Error(
        JSON.stringify({
          code: "INVALID_REQUEST",
          message: "question 'q_req_1' was not found",
          details: { reason: "QUESTION_NOT_FOUND" },
        }),
      ),
    );
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await result.current.answer("q_req_1", { deploy_target: ["Vercel"] });
    });

    expect(result.current.questions).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it("drops a card already answered or expired elsewhere", async () => {
    resolveRejects(
      new Error(
        JSON.stringify({
          code: "INVALID_REQUEST",
          message: "question 'q_req_1' is already expired",
          details: { reason: "QUESTION_ALREADY_TERMINAL" },
        }),
      ),
    );
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await result.current.answer("q_req_1", { deploy_target: ["Vercel"] });
    });

    expect(result.current.questions).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it("keeps the card on a rejected answer the operator can fix", async () => {
    resolveRejects(
      new Error(
        JSON.stringify({
          code: "INVALID_REQUEST",
          message: "question 'stripe_key' requires exactly one secret value",
          details: { reason: "QUESTION_INVALID_ANSWER" },
        }),
      ),
    );
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await expect(
        result.current.answer("q_req_1", { deploy_target: [] }),
      ).rejects.toThrow(/QUESTION_INVALID_ANSWER/);
    });

    expect(result.current.questions).toHaveLength(1);
  });

  it("surfaces other errors and keeps the card", async () => {
    resolveRejects(new Error("network down"));
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", record()));

    await act(async () => {
      await expect(
        result.current.answer("q_req_1", { deploy_target: ["Vercel"] }),
      ).rejects.toThrow("network down");
    });

    expect(result.current.questions).toHaveLength(1);
    expect(result.current.error).toBe("network down");
  });

  it("seeds pending questions from question.list on connect (reload recovery)", async () => {
    sendReqMock = vi.fn().mockResolvedValue({
      questions: [
        record({ id: "q_seed" }),
        record({ id: "q_done", status: "answered" }),
        record({ id: "q_other", sessionKey: "agent:agent_x:user_other" }),
      ],
    });
    const { result } = renderHook(() => useQuestions());

    await waitFor(() =>
      expect(sendReqMock).toHaveBeenCalledWith("question.list", {}),
    );
    await waitFor(() => expect(result.current.questions).toHaveLength(1));
    expect(result.current.questions[0].id).toBe("q_seed");
  });

  it("reconciles pending questions when the AppSync connection revision advances", async () => {
    const view = renderHook(() => useQuestions());
    await waitFor(() => expect(sendReqMock).toHaveBeenCalledWith("question.list", {}));
    sendReqMock.mockClear();

    connectionRevision = 2;
    view.rerender();

    await waitFor(() => expect(sendReqMock).toHaveBeenCalledWith("question.list", {}));
  });

  it("a question.list failure is swallowed (parked/asleep container)", async () => {
    sendReqMock = vi.fn().mockRejectedValue(new Error("timeout"));
    const { result } = renderHook(() => useQuestions());
    await waitFor(() => expect(sendReqMock).toHaveBeenCalled());
    expect(result.current.questions).toHaveLength(0);
    expect(result.current.error).toBeNull();
  });

  it("secret answers carry secretStoreAllowedHosts only when supplied", async () => {
    const secret = record({
      id: "q_secret",
      questions: [
        {
          questionId: "stripe_key",
          header: "Stripe",
          question: "Paste your Stripe API key.",
          options: [],
          isSecret: true,
          secretStore: { name: "STRIPE_API_KEY", kind: "secret", allowedHosts: ["api.stripe.com"] },
        },
      ],
    });
    const { result } = renderHook(() => useQuestions());
    act(() => emit("question.requested", secret));

    await act(async () => {
      await result.current.answer("q_secret", { stripe_key: ["sk_live_x"] }, [
        "api.stripe.com",
      ]);
    });

    expect(sendReqMock).toHaveBeenCalledWith("question.resolve", {
      id: "q_secret",
      answers: { answers: { stripe_key: ["sk_live_x"] } },
      secretStoreAllowedHosts: ["api.stripe.com"],
    });
  });
});
