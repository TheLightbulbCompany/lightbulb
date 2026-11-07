"use client";

import { useState } from "react";
import Link from "next/link";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { copyText } from "./messageLinks";

const COMMANDS = [{ tool: "Claude Code", command: "lightbulb claude" }, { tool: "Codex", command: "lightbulb codex" }];

/**
 * How sessions are shared: one started with Lightbulb is shared from its start, with its live
 * terminal here; any other one only when its person shares it.
 */
export function StartSessionDialog() {
  const [copied, setCopied] = useState<string | null>(null);
  return <Dialog>
    <DialogTrigger asChild><Button variant="outline" size="sm">Start a session</Button></DialogTrigger>
    <DialogContent showCloseButton>
      <DialogHeader>
        <DialogTitle>Share a session</DialogTitle>
        <DialogDescription>A session you start with Lightbulb is shared with this workspace, with its live terminal here. Any other session is only yours until you choose Share to Lightbulb in it or turn on Share every new session.</DialogDescription>
      </DialogHeader>
      <ol className="list-decimal space-y-3 pl-5 text-ui">
        <li>In the Lightbulb desktop app on your Mac, turn on sharing in <Link className="underline underline-offset-4" href="/settings?panel=desktop">Settings → Desktop → Session sharing</Link>. You only do this once.</li>
        <li>Use New session in the desktop app, or run one of these in a terminal:
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
        <li>A plain <code>claude</code> or <code>codex</code> is shared only when Share every new session is on, or after you choose Share to Lightbulb in it (<code>/lightbulb:share</code>).</li>
      </ol>
      <p className="text-caption text-muted-foreground">Codex appears after your first prompt.</p>
    </DialogContent>
  </Dialog>;
}
