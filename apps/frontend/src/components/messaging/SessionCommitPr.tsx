"use client";

import { useState } from "react";
import { useAuth } from "@clerk/nextjs";
import { GitPullRequest } from "lucide-react";
import { canControlSession, COMMIT_AND_PR_INSTRUCTION, useSessionCommand, type LocalSession, type SessionSource } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";

/**
 * "Commit & open PR", from the "What changed" summary: one follow-up instruction to the session, never a git
 * operation of ours. The harness does the work on the owner's Mac with the
 * owner's own permissions -- a push or `gh pr create` still asks for approval
 * there (or on the approval card) like any other command.
 */
export function SessionCommitPr({ session, client, own, ownerName }: {
  session: LocalSession; client: SessionSource["client"]; own: boolean; ownerName: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const command = useSessionCommand(client, session, { uuid: () => crypto.randomUUID(), onAccessLost: () => {}, onSent: () => setConfirming(false) });
  const { userId } = useAuth();
  if (!canControlSession(session, own, userId) || !command.canSend || session.status === "offline") return null;
  const tool = session.provider === "claude" ? "Claude" : "Codex";
  return <>
    <Button type="button" variant="outline" size="sm" disabled={command.locked} onClick={() => setConfirming(true)}>
      <GitPullRequest aria-hidden="true" />Commit &amp; open PR
    </Button>
    {command.chip && <span role="status" className="text-xs text-muted-foreground" title={command.chip.detail ?? undefined}>{command.chip.label}</span>}
    <Dialog open={confirming} onOpenChange={setConfirming}>
      <DialogContent className="max-w-[calc(100%-2rem)] sm:max-w-md">
        <DialogHeader><DialogTitle>Commit and open a pull request?</DialogTitle>
          <DialogDescription>
            This asks {tool} to commit this session&apos;s changes on a new branch, push it and open a pull request, on {own ? "your" : `${ownerName}'s`} Mac
            with {own ? "your" : "their"} git and GitHub credentials. Lightbulb runs no git itself; any push or PR command still needs approval.
          </DialogDescription></DialogHeader>
        <pre className="whitespace-pre-wrap rounded bg-muted p-2 text-xs">{COMMIT_AND_PR_INSTRUCTION}</pre>
        {command.attempt?.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
          <Button type="button" disabled={command.locked} onClick={() => command.send(COMMIT_AND_PR_INSTRUCTION)}>Send to {tool}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>;
}
