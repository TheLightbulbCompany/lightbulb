/**
 * Fire the visible first-run kickoff prompt exactly once — ported from the
 * web (apps/frontend/src/hooks/useFirstRunKickoff.ts); keep semantics in
 * sync.
 *
 * Write-before-send ordering is load-bearing: `writeFirstRun({started:true})`
 * must complete before `send` is called — Clerk metadata writes are
 * read-modify-write, so racing writers could drop `started` and cause a
 * duplicate kickoff on the next launch.
 *
 * On failure we log and reset the `fired` guard so a later re-render can
 * retry — better no kickoff than one that can never fire again. A write
 * rejection must NOT fall through to send. A send that dispatches nothing
 * additionally rolls the `started` write back: it already persisted, and
 * without the rollback every later guard (this hook AND the roster
 * redirect) reads `started: true` and the kickoff is lost forever. A false
 * send means NOTHING went out, so the retry cannot double-send.
 *
 * The kickoff waits for a live gateway connection before it marks `started`
 * and sends: a send queued behind a dead socket resolves locally but may
 * never reach the agent, and with `started` already persisted the kickoff
 * would be permanently lost.
 *
 * The connection gate narrows that lost-send window but cannot close it (an
 * app backgrounded mid-flight, a socket that dies after the local resolve), so
 * `started` is NOT trusted as proof the kickoff landed. It is a cache of the
 * thread; the thread is ground truth. When a mount finds `started && !completed`
 * but the default agent's history is confirmed loaded AND empty, the kickoff
 * was lost and is re-fired once (`historyLoaded`/`historyEmpty` from the
 * caller's chat state). The write-before-send order is kept — the re-fire's
 * write is idempotent — so the duplicate-kickoff race the ordering guards
 * against stays closed.
 */
import { useEffect, useRef } from "react";
import type { useUser } from "@clerk/clerk-expo";

import { FIRST_RUN_PROMPT, readFirstRun, writeFirstRun } from "../../lib/firstRun";

type UserResource = NonNullable<ReturnType<typeof useUser>["user"]>;

export function useFirstRunKickoff(opts: {
  user: UserResource | null | undefined;
  agentId: string | null;
  defaultId: string | null | undefined;
  isConnected: boolean;
  historyLoaded: boolean;
  historyEmpty: boolean;
  /** Full local send pipeline (bubble + busy + dispatch); false = not sent. */
  send: (message: string) => boolean;
}) {
  const { user, agentId, defaultId, isConnected, historyLoaded, historyEmpty, send } =
    opts;
  const fired = useRef(false);
  useEffect(() => {
    if (fired.current || !user) return;
    if (!isConnected) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted) return;
    if (fr.started) {
      // Lost-kickoff reconciliation (web parity): `started` is written BEFORE
      // the send (Clerk read-modify-write forces that order), so a send that
      // dies in flight (backgrounded app, dead socket) leaves started=true with
      // no message ever delivered — and the kickoff would be skipped forever.
      // The thread is ground truth: only skip when the kickoff is completed OR
      // the history actually shows messages.
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
        if (!send(FIRST_RUN_PROMPT)) {
          await writeFirstRun(user, { started: false });
          throw new Error("kickoff send failed");
        }
      } catch (err) {
        console.error("first-run kickoff failed", err);
        fired.current = false;
      }
    })();
  }, [user, agentId, defaultId, isConnected, historyLoaded, historyEmpty, send]);
}
