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
 */
export function useFirstRunKickoff(opts: {
  user: UserResource | null | undefined;
  agentId: string | null;
  defaultId: string | null | undefined;
  sendMessage: (message: string) => Promise<void>;
}) {
  const { user, agentId, defaultId, sendMessage } = opts;
  const fired = React.useRef(false);
  React.useEffect(() => {
    if (fired.current || !user) return;
    const fr = readFirstRun(user);
    if (!fr.briefSubmitted || fr.started) return;
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
  }, [user, agentId, defaultId, sendMessage]);
}
