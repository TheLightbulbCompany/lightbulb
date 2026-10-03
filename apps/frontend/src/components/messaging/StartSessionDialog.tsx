"use client";

import { useState } from "react";
import Link from "next/link";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { copyText } from "./messageLinks";

const COMMANDS = [{ tool: "Claude Code", command: "lightbulb claude" }, { tool: "Codex", command: "lightbulb codex" }];

/**
 * How to start a shared session. Only sessions started in Lightbulb are shared: New session in the
 * desktop app, or `lightbulb claude|codex` in a terminal. A plain `claude` or `codex` stays private.
 */
export function StartSessionDialog() {
  const [copied, setCopied] = useState<string | null>(null);
  return <Dialog>
    <DialogTrigger asChild><Button variant="outline" size="sm">Start a session</Button></DialogTrigger>
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Start a shared session</DialogTitle>
        <DialogDescription>Only sessions you start in Lightbulb are shared. A plain <code>claude</code> or <code>codex</code> session stays private.</DialogDescription>
      </DialogHeader>
      <ol className="list-decimal space-y-3 pl-5 text-sm">
        <li>In the Lightbulb desktop app on your Mac, turn on sharing in <Link className="underline underline-offset-4" href="/settings">Settings → Native terminal sessions</Link>. You only do this once.</li>
        <li>Use New session in the desktop app, or open a terminal in a project inside an approved folder and run one of these:
          <ul className="mt-2 space-y-2">{COMMANDS.map(({ tool, command }) => <li key={command} className="flex items-center gap-2">
            <code className="rounded bg-muted px-2 py-1 font-mono">{command}</code>
            <span className="text-muted-foreground">{tool}</span>
            <Button variant="ghost" size="icon-sm" className="ml-auto" aria-label={`Copy ${command}`}
              onClick={() => void copyText(command).then((ok) => { if (ok) setCopied(command); })}>
              {copied === command ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
            </Button>
          </li>)}</ul>
        </li>
        <li>It shows up under Sessions. Everyone in this workspace can watch it; only you can send to it.</li>
      </ol>
      <p className="text-xs text-muted-foreground">Codex appears after your first prompt.</p>
    </DialogContent>
  </Dialog>;
}
