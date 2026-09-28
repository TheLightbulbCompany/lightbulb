"use client";

import { useId, useRef, useState } from "react";
import { ArrowDownToLine } from "lucide-react";
import { pickupSource, pickupStatus, sessionTitle, type LocalSession } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { canPickUpHere, useSessionPickup, type PickupClient } from "./useSessionPickup";

/** A session can be picked up once its owner allowed it and a snapshot exists. */
export const canPickUp = (session: LocalSession) => session.pickup?.allowed === true && session.pickup.state === "ready";

/**
 * "Pick up": continue a teammate's conversation on this Mac, with the files it
 * changed. The dialog says what will happen before anything does, because it
 * writes into the person's own repository and opens their Terminal.
 */
export function SessionPickup({ session, ownerName, own, client, variant = "default" }: {
  session: LocalSession; ownerName: string; own: boolean; client: PickupClient;
  /** `quiet` for a secondary placement, where it must not compete with the screen's own action. */
  variant?: "default" | "quiet";
}) {
  const [open, setOpen] = useState(false);
  const [folder, setFolder] = useState<string | null>(null);
  const pickup = useSessionPickup(client, session);
  const group = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  if (!canPickUp(session) || !client.requestLocalSessionPickup) return null;
  const { state } = pickup;
  const here = canPickUpHere();
  // Only Claude Code sessions can be picked up (the backend refuses the rest).
  const tool = "Claude Code";
  const whose = own ? "your" : `${ownerName}'s`;
  const source = pickupSource(session.pickup);
  const close = (next: boolean) => {
    // Closing mid-import would hide where the session went; the Mac finishes either way.
    if (!next && state.phase === "working") return;
    setOpen(next);
    if (!next) { pickup.reset(); setFolder(null); }
  };

  return <>
    <Button ref={trigger} type="button" size="sm" variant={variant === "quiet" ? "outline" : "default"} onClick={() => setOpen(true)}>
      <ArrowDownToLine aria-hidden="true" />Pick up
    </Button>
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md max-h-[calc(100dvh-2rem)] overflow-y-auto text-sm"
        onCloseAutoFocus={event => { event.preventDefault(); trigger.current?.focus(); }}>
        <DialogHeader>
          <DialogTitle>{state.phase === "done" ? "Picked up" : "Pick up this session"}</DialogTitle>
          <DialogDescription>{sessionTitle(session)}</DialogDescription>
        </DialogHeader>

        {!here && <p>Open Lightbulb on your Mac to pick this up. The session continues in your own Terminal, so it can only start from the desktop app.</p>}

        {here && state.phase === "idle" && <>
          <p>Continue {whose} conversation on this Mac, from where it stands now.</p>
          <ul className="session-pickup-steps">
            <li>{source
              ? <>Adds a new branch and folder to your copy of <span className="session-fact-value">{source}</span>, holding everything the session changed. Your own work there is not touched.</>
              : <>This session has no repository, so only the conversation comes over. You choose the folder it continues in.</>}</li>
            <li>Opens {tool} in Terminal, under your own account.</li>
            <li>{own ? "The original session" : `${ownerName}'s session`} carries on unchanged. Yours is a separate copy from this point.</li>
          </ul>
          <p className="text-muted-foreground">{pickupStatus(session.pickup, timeAgo)}</p>
        </>}

        {state.phase === "working" && <p role="status">{state.step === "asking" ? "Asking for the snapshot…" : "Downloading it and opening Terminal…"}</p>}

        {state.phase === "choose" && <fieldset className="grid gap-2">
          <legend className="mb-1">{state.why === "several" ? "This Mac has more than one copy of that repository. Which one should it continue in?" : "Which folder should the conversation continue in?"}</legend>
          {state.folders.length === 0 && <p className="text-muted-foreground">This Mac has no approved folders. Add one in Settings, under Desktop, then pick the session up again.</p>}
          {state.folders.map(path => <label key={path} className="flex items-center gap-2">
            <input type="radio" name={group} value={path} checked={folder === path} onChange={() => setFolder(path)} />
            <span className="session-fact-value room-wrap">{path}</span>
          </label>)}
        </fieldset>}

        {state.phase === "done" && <div role="status" className="grid gap-2">
          <p>{tool} is open in Terminal{state.conversationOnly ? "." : state.branch ? <>, on a new branch <span className="session-fact-value">{state.branch}</span>.</> : "."}</p>
          {state.worktree && <p className="session-fact-value room-wrap text-muted-foreground">{state.worktree}</p>}
          {/* The snapshot can carry a teammate's project settings, and those can run commands on start. */}
          {state.ownSettingsOnly && !state.conversationOnly && <p>It opened with your own Claude settings only. Settings files and hooks that came with the snapshot were not loaded; read them before you start Claude there yourself.</p>}
          <p className="text-muted-foreground">It appears in your sessions as soon as it starts.</p>
        </div>}

        {state.phase === "failed" && <p role="alert" className="text-warn">{state.message}</p>}

        <DialogFooter>
          {state.phase === "done" || !here
            ? <Button type="button" onClick={() => close(false)}>Done</Button>
            : <>
              <Button type="button" variant="ghost" disabled={state.phase === "working"} onClick={() => close(false)}>Cancel</Button>
              {state.phase === "choose"
                ? <Button type="button" disabled={!folder} onClick={() => folder && void pickup.start(folder)}>Pick up here</Button>
                : <Button type="button" disabled={state.phase === "working"} onClick={() => void pickup.start()}>{state.phase === "failed" ? "Try again" : "Pick up"}</Button>}
            </>}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
