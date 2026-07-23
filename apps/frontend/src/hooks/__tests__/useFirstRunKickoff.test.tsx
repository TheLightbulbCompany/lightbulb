import { describe, expect, it, vi } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

import { capture } from "@/lib/analytics";
import { useFirstRunKickoff } from "@/hooks/useFirstRunKickoff";
import { FIRST_RUN_PROMPT } from "@/lib/firstRun";

function fakeUser(
  firstRun: Record<string, unknown>,
  update = vi.fn().mockResolvedValue(undefined),
) {
  return {
    unsafeMetadata: { firstRun },
    update,
  } as never;
}

// Flush the microtask queue without a real timer, so a (hypothetically)
// unguarded second kickoff's `await writeFirstRun(...); await sendMessage(...)`
// chain has room to run before we assert on it.
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}

describe("useFirstRunKickoff", () => {
  it("sends the prompt once when brief submitted and not started", async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true });
    const { rerender } = renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    rerender();
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("blocks a genuine second effect run via the `fired` ref guard", async () => {
    // A same-props rerender (the test above) never re-invokes the effect
    // body at all — React's dep-comparison bails out before the guard is
    // ever consulted, so that test would still pass with `fired.current`
    // deleted entirely. Here we pass a NEW `sendMessage` function reference
    // on the second render, which changes the effect's dependency array and
    // forces React to genuinely re-run the effect body. The only thing that
    // can stop a second send at that point is the `fired.current` ref guard.
    //
    // The mocked `update` below is a no-op that does NOT mutate `user`, so
    // `user.unsafeMetadata.firstRun` stays `{ briefSubmitted: true }`
    // (no `started`) across both renders. That isolates the REF guard: the
    // durable-flag (`started`) gate can't be what blocks the second send,
    // because as far as the hook's next read is concerned, `started` was
    // never persisted.
    //
    // sendMessage1's promise is deliberately left UNRESOLVED until after the
    // second render: rerender() is synchronous (React flushes effects before
    // it returns), so calling it while the first kickoff is still in flight
    // forces the second effect's synchronous top-of-effect guard check to
    // run at the earliest possible moment — the tightest race window for
    // `fired.current` to have not been set yet. A `waitFor(...)`-then-rerender
    // shape (with an eagerly-resolving mock) would let the whole first
    // kickoff — including a guard set anywhere inside it — finish before the
    // test ever calls rerender, which would hide a "guard set too late" bug.
    let resolveSend1: (() => void) | undefined;
    const sendMessage1 = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolveSend1 = resolve;
        }),
    );
    const user = fakeUser({ briefSubmitted: true });

    const { rerender } = renderHook(
      ({ sendMessage }) =>
        useFirstRunKickoff({
          user,
          agentId: "a1",
          defaultId: "a1",
          isConnected: true,
          historyLoaded: true,
          historyEmpty: true,
          sendMessage,
        }),
      { initialProps: { sendMessage: sendMessage1 } },
    );
    await waitFor(() => expect(sendMessage1).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(sendMessage1).toHaveBeenCalledTimes(1);

    const sendMessage2 = vi.fn().mockResolvedValue(undefined);
    rerender({ sendMessage: sendMessage2 });
    await flushMicrotasks();

    expect(sendMessage2).not.toHaveBeenCalled();
    expect(sendMessage1).toHaveBeenCalledTimes(1);

    // Let the first kickoff's pending promise settle so it doesn't leak
    // into other tests.
    resolveSend1?.();
    await flushMicrotasks();
  });

  it("waits for a live connection before firing, then fires once connected", async () => {
    // While disconnected, the kickoff must NOT run — marking `started` before
    // a durable dispatch is possible would lose the prompt to the in-memory
    // reconnect queue and permanently suppress a retry.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true });
    const { rerender } = renderHook(
      ({ isConnected }) =>
        useFirstRunKickoff({
          user,
          agentId: "a1",
          defaultId: "a1",
          isConnected,
          historyLoaded: true,
          historyEmpty: true,
          sendMessage,
        }),
      { initialProps: { isConnected: false } },
    );
    await flushMicrotasks();
    expect(sendMessage).not.toHaveBeenCalled();

    // Connection establishes → effect re-runs → kickoff fires exactly once.
    rerender({ isConnected: true });
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("does nothing on a non-default agent window", () => {
    const sendMessage = vi.fn();
    const user = fakeUser({ briefSubmitted: true });
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a2",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does not send or capture when the metadata write rejects", async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const update = vi.fn().mockRejectedValue(new Error("network down"));
    const user = fakeUser({ briefSubmitted: true }, update);
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );

    // Wait a tick for the rejected write's catch handler to run.
    await waitFor(() =>
      expect(errorSpy).toHaveBeenCalledWith("first-run kickoff failed", expect.any(Error)),
    );
    expect(sendMessage).not.toHaveBeenCalled();
    expect(capture).not.toHaveBeenCalled();

    errorSpy.mockRestore();
  });

  it("reconciles a lost kickoff: started but thread confirmed empty → re-fires", async () => {
    // Verified prod incident: `started` is written BEFORE the send, so a send
    // that dies in flight (tab closed, WS queue death) leaves started=true with
    // an empty thread and no bootstrap run — stranding the aha flow forever.
    // A mount that confirms the default thread is loaded AND empty must treat
    // the latch as lost and fire the kickoff once more.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true, started: true });
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(sendMessage).toHaveBeenCalledTimes(1);
  });

  it("does not re-fire once the kickoff is completed, even on an empty thread", async () => {
    // `completed` is the terminal truth — a compacted/cleared session can be
    // legitimately empty after a completed kickoff. Never re-fire past it.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true, started: true, completed: true });
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );
    await flushMicrotasks();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("does not re-fire when started and the thread already has messages", async () => {
    // started=true with a NON-empty thread means the kickoff really landed —
    // the latch is confirmed by the thread, so it must be trusted.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true, started: true });
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: false,
        sendMessage,
      }),
    );
    await flushMicrotasks();
    expect(sendMessage).not.toHaveBeenCalled();
  });

  it("defers while history is still loading: no re-fire and no metadata write", async () => {
    // An unloaded thread is not evidence of a lost kickoff — judging it empty
    // would re-fire against a thread that may already hold the bootstrap run.
    // Defer: no send AND no (idempotent) started write until history settles.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const update = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true, started: true }, update);
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: false,
        historyEmpty: true,
        sendMessage,
      }),
    );
    await flushMicrotasks();
    expect(sendMessage).not.toHaveBeenCalled();
    expect(update).not.toHaveBeenCalled();
  });

  it("fresh kickoff writes started:true before sending", async () => {
    // Regression: the not-started path is unchanged by reconciliation — it
    // still persists `started` (write-before-send) and then sends.
    const sendMessage = vi.fn().mockResolvedValue(undefined);
    const update = vi.fn().mockResolvedValue(undefined);
    const user = fakeUser({ briefSubmitted: true }, update);
    renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a1",
        defaultId: "a1",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        sendMessage,
      }),
    );
    await waitFor(() => expect(sendMessage).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(update).toHaveBeenCalledWith(
      expect.objectContaining({
        unsafeMetadata: expect.objectContaining({
          firstRun: expect.objectContaining({ started: true }),
        }),
      }),
    );
  });
});
