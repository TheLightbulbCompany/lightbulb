"use client";
import * as React from "react";
import type { useUser } from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { FIRST_RUN_PROMPT, readFirstRun, writeFirstRun } from "@/lib/firstRun";

/**
 * Clerk's UserResource, derived from useUser() — @clerk/types is not a
 * direct/hoisted dependency of this package (only reachable transitively
 * through @clerk/nextjs), so it is not directly importable here. Mirrors
 * the same derived-type pattern used in src/lib/firstRun.ts.
 */
type UserResource = NonNullable<ReturnType<typeof useUser>["user"]>;

/**
 * Fire the visible first-run kickoff prompt exactly once (spec §4.C).
 *
 * Write-before-send ordering is load-bearing: `writeFirstRun({started:true})`
 * must complete before `sendMessage` is called. `sendMessage` triggers
 * useGetStarted's own unsafeMetadata write via emitStep("chat"); Clerk
 * metadata writes are read-modify-write, so a concurrent write could drop
 * `started` and cause a duplicate kickoff on the next reload.
 *
 * On write/send failure we log and reset the `fired` guard so a later
 * remount can retry — better no kickoff than one that can never fire again.
 * A write rejection must NOT fall through to send.
 *
 * The kickoff waits for a live gateway connection (`isConnected`) before it
 * marks `started` and sends. Without this, an effect that runs mid-(re)connect
 * would hit `sendMessage`'s in-memory waiting-reconnect queue: the send
 * resolves locally but the prompt may never reach the agent, and because
 * `started` is already persisted a reload would permanently skip the kickoff.
 * Gating on the connection defers the whole attempt until a durable dispatch
 * is possible; the effect re-runs when `isConnected` flips true.
 *
 * The connection gate narrows that lost-send window but cannot close it (a tab
 * closed mid-flight, a socket that dies after the local resolve), so `started`
 * is NOT trusted as proof the kickoff landed. It is a cache of the thread; the
 * thread is ground truth. When a mount finds `started && !completed` but the
 * default agent's history is confirmed loaded AND empty, the kickoff was lost
 * and is re-fired once (`historyLoaded`/`historyEmpty` from the caller's chat
 * state). The write-before-send order is kept — the re-fire's write is
 * idempotent — so the duplicate-kickoff race the ordering guards against stays
 * closed.
 */
export function useFirstRunKickoff(opts: {
  user: UserResource | null | undefined;
  agentId: string | null;
  defaultId: string | null | undefined;
  isConnected: boolean;
  historyLoaded: boolean;
  historyEmpty: boolean;
  sendMessage: (message: string) => Promise<void>;
}) {
  const { user, agentId, defaultId, isConnected, historyLoaded, historyEmpty, sendMessage } = opts;
  const fired = React.useRef(false);
  React.useEffect(() => {
    if (fired.current || !user) return;
    if (!isConnected) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted) return;
    if (fr.started) {
      // Lost-kickoff reconciliation: `started` is written BEFORE the send
      // (Clerk read-modify-write races force that order), so a send that
      // dies in flight (tab closed, WS queue death) leaves started=true
      // with no message ever delivered — and the kickoff would be skipped
      // forever (verified prod incident: started=true, empty thread, no
      // bootstrap run). The thread is ground truth: only skip when the
      // kickoff is completed OR the history actually shows messages.
      if (fr.completed) return;
      if (!historyLoaded) return; // don't judge an unloaded thread
      if (!historyEmpty) return; // messages exist — kickoff really ran
      // started && !completed && confirmed-empty thread → re-fire below.
    }
    if (!agentId || !defaultId || agentId !== defaultId) return;
    fired.current = true;
    void (async () => {
      try {
        await writeFirstRun(user, { started: true });
        await sendMessage(FIRST_RUN_PROMPT);
        capture("first_run_kickoff_sent");
      } catch (err) {
        console.error("first-run kickoff failed", err);
        fired.current = false;
      }
    })();
  }, [user, agentId, defaultId, isConnected, historyLoaded, historyEmpty, sendMessage]);
}
