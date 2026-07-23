import { renderHook, waitFor } from "@testing-library/react-native";

import { FIRST_RUN_PROMPT } from "../../../lib/firstRun";
import { useFirstRunKickoff } from "../useFirstRunKickoff";

function makeUser(firstRun: Record<string, unknown>) {
  return {
    unsafeMetadata: { firstRun },
    update: jest.fn(async () => undefined),
  } as never;
}

// Flush the microtask queue without a real timer, so the effect's async
// write-then-send chain has room to run before a "did not fire" assertion.
async function flushMicrotasks(): Promise<void> {
  for (let i = 0; i < 10; i++) {
    await Promise.resolve();
  }
}

describe("useFirstRunKickoff", () => {
  it("stamps started THEN sends the visible prompt, exactly once", async () => {
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "agent_friday",
        defaultId: "agent_friday",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );

    await waitFor(() => expect(send).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    const update = (user as { update: jest.Mock }).update;
    expect(update).toHaveBeenCalledWith({
      unsafeMetadata: { firstRun: { briefSubmitted: true, started: true } },
    });
    // Write-before-send ordering is load-bearing (see hook docstring).
    expect(update.mock.invocationCallOrder[0]).toBeLessThan(
      send.mock.invocationCallOrder[0],
    );
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("rolls back started when the send dispatches nothing", async () => {
    // Mobile-only nuance the web hook lacks: `send` is synchronous and returns
    // false when nothing dispatched. It composes with the reconcile below —
    // this is still the fresh (not-started) path.
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => false);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );
    const update = (user as { update: jest.Mock }).update;
    await waitFor(() => expect(update).toHaveBeenCalledTimes(2));
    expect(update).toHaveBeenLastCalledWith({
      unsafeMetadata: { firstRun: { briefSubmitted: true, started: false } },
    });
  });

  it("does nothing for a non-default agent", async () => {
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "agent_other",
        defaultId: "agent_friday",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();
  });

  it("does nothing without a brief", async () => {
    const user = makeUser({});
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();
  });

  it("waits for a live connection before firing, then fires once connected", async () => {
    // Marking `started` before a durable dispatch is possible would lose the
    // prompt to the in-memory reconnect queue and permanently suppress a retry.
    const user = makeUser({ briefSubmitted: true });
    const send = jest.fn(() => true);
    const { rerender } = await renderHook(
      ({ isConnected }: { isConnected: boolean }) =>
        useFirstRunKickoff({
          user,
          agentId: "a",
          defaultId: "a",
          isConnected,
          historyLoaded: true,
          historyEmpty: true,
          send,
        }),
      { initialProps: { isConnected: false } },
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();

    await rerender({ isConnected: true });
    await waitFor(() => expect(send).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("reconciles a lost kickoff: started but thread confirmed empty → re-fires", async () => {
    // Verified prod incident (web PR #1391): `started` is written BEFORE the
    // send, so a send that dies in flight (backgrounded app, dead socket)
    // leaves started=true over an empty thread with no bootstrap run —
    // stranding the aha flow forever. A mount that confirms the default thread
    // is loaded AND empty treats the latch as lost and fires the kickoff once
    // more.
    const user = makeUser({ briefSubmitted: true, started: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );
    await waitFor(() => expect(send).toHaveBeenCalledWith(FIRST_RUN_PROMPT));
    expect(send).toHaveBeenCalledTimes(1);
  });

  it("does not re-fire once the kickoff is completed, even on an empty thread", async () => {
    // `completed` is the terminal truth — a compacted/cleared session can be
    // legitimately empty after a completed kickoff. Never re-fire past it.
    const user = makeUser({ briefSubmitted: true, started: true, completed: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: true,
        send,
      }),
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();
  });

  it("does not re-fire when started and the thread already has messages", async () => {
    // started=true with a NON-empty thread means the kickoff really landed —
    // the latch is confirmed by the thread, so it must be trusted.
    const user = makeUser({ briefSubmitted: true, started: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: true,
        historyEmpty: false,
        send,
      }),
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();
  });

  it("defers while history is still loading: no re-fire and no metadata write", async () => {
    // An unloaded thread is not evidence of a lost kickoff — judging it empty
    // would re-fire against a thread that may already hold the bootstrap run.
    // Defer: no send AND no (idempotent) started write until history settles.
    const user = makeUser({ briefSubmitted: true, started: true });
    const send = jest.fn(() => true);
    await renderHook(() =>
      useFirstRunKickoff({
        user,
        agentId: "a",
        defaultId: "a",
        isConnected: true,
        historyLoaded: false,
        historyEmpty: true,
        send,
      }),
    );
    await flushMicrotasks();
    expect(send).not.toHaveBeenCalled();
    expect((user as { update: jest.Mock }).update).not.toHaveBeenCalled();
  });
});
