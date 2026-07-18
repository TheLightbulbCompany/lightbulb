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
 * On write/send failure we log and reset the `fired` guard so a later
 * re-render can retry — better no kickoff than one that can never fire
 * again. A write rejection must NOT fall through to send.
 *
 * The kickoff waits for a live gateway connection before it marks `started`
 * and sends: a send queued behind a dead socket resolves locally but may
 * never reach the agent, and with `started` already persisted the kickoff
 * would be permanently lost.
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
  /** Full local send pipeline (bubble + busy + dispatch); false = not sent. */
  send: (message: string) => boolean;
}) {
  const { user, agentId, defaultId, isConnected, send } = opts;
  const fired = useRef(false);
  useEffect(() => {
    if (fired.current || !user) return;
    if (!isConnected) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted || fr.started) return;
    if (!agentId || !defaultId || agentId !== defaultId) return;
    fired.current = true;
    void (async () => {
      try {
        await writeFirstRun(user, { started: true });
        if (!send(FIRST_RUN_PROMPT)) throw new Error("kickoff send failed");
      } catch (err) {
        console.error("first-run kickoff failed", err);
        fired.current = false;
      }
    })();
  }, [user, agentId, defaultId, isConnected, send]);
}
