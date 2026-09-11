"use client";
import { useUser } from "@clerk/nextjs";
import { capture } from "@/lib/analytics";
import { readFirstRun } from "@/lib/firstRun";
import { GetStartedLauncher } from "./GetStartedLauncher";
import { GetStartedCard } from "./GetStartedCard";
import { useGetStarted } from "./useGetStarted";
import type { StepId } from "./getStartedConfig";

export interface GetStartedActions { chat: () => void; task: () => void; hire: () => void; channel: () => void }

export function GetStarted({ derived, actions }: { derived: Partial<Record<StepId, boolean>>; actions: GetStartedActions }): React.ReactElement | null {
  const { user } = useUser();
  const firstRun = readFirstRun(user);
  // First-run users saw their CEO do the checklist's steps (spec §7) —
  // "what the CEO already did" supersedes "what you must do". Gate BEFORE
  // useGetStarted (below, in GetStartedGuide) so a suppressed user never
  // mounts the hook and thus incurs none of its side effects: auto-open, the
  // get_started_opened capture, or the emitStep("chat") checklist progress
  // that the auto-kickoff chat would otherwise persist.
  if (firstRun.briefSubmitted) return null;
  return <GetStartedGuide derived={derived} actions={actions} />;
}

function GetStartedGuide({ derived, actions }: { derived: Partial<Record<StepId, boolean>>; actions: GetStartedActions }): React.ReactElement | null {
  const gs = useGetStarted({ derived });
  const complete = gs.progress.total > 0 && gs.progress.done === gs.progress.total;
  if (gs.dismissed || gs.completedFired || complete) return null;

  const onAction = (id: Exclude<StepId, "meet">) => {
    capture("get_started_step_clicked", { step: id });
    gs.minimize();            // hand off: card steps aside, destination opens
    actions[id]();
  };

  return (
    <>
      <GetStartedLauncher done={gs.progress.done} total={gs.progress.total} pulse={!gs.seen} onClick={gs.open} />
      {gs.isOpen && !gs.minimized && <GetStartedCard gs={gs} onAction={onAction} />}
    </>
  );
}
