"use client";

import { useCallback, useEffect, useState } from "react";

import { bridge } from "@/components/messaging/localTerminal";

type Status = {
  state: "hidden" | "off" | "connected" | "connecting" | "optedOut" | "failed" | "agentShell";
  workspace: string | null;
  notice: string | null;
  connectLimited?: boolean;
  leaveLimited?: boolean;
};

const REOPEN = "Reopen Lightbulb to try again";
const row = { padding: "4px 12px", fontSize: "12px", color: "var(--warn)", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" } as const;
const dot = { width: "6px", height: "6px", borderRadius: "50%", backgroundColor: "currentColor", display: "inline-block" } as const;
const link = { background: "none", border: 0, padding: 0, color: "inherit", textDecoration: "underline", cursor: "pointer", font: "inherit" } as const;

/**
 * The desktop footer's network line: connecting, a Connect button, or the once-only notice.
 * Every action is a native command with no arguments; the page never holds a ticket.
 * Nothing in a browser or an older app; `off` belongs to the background-service fault (Step 3).
 */
export function NetworkConnect() {
  const [status, setStatus] = useState<Status | null>(null);
  const [gone, setGone] = useState(false);
  const [tick, setTick] = useState(0);
  const poll = useCallback(() => setTick((n) => n + 1), []);

  useEffect(() => {
    const invoke = bridge();
    if (!invoke || gone) return;
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    // ponytail: a poll (3 s while connecting, else 60 s); an event from the app if that ever shows.
    Promise.resolve(invoke("network_status")).then(
      (s) => { if (live) { setStatus(s as Status); timer = setTimeout(poll, (s as Status | undefined)?.state === "connecting" ? 3000 : 60_000); } },
      (e) => {
        if (!live) return;
        if (/command\b.*\bnot found|unknown command/i.test(String(e))) return setGone(true);
        timer = setTimeout(poll, 60_000);
      },
    );
    return () => { live = false; clearTimeout(timer); };
  }, [tick, gone, poll]);

  // A refusal, including Cancel on the native alert, only re-reads the state. After a Cancel the
  // alert is used up until relaunch, and the state says so: that replaces the button.
  const act = (command: string) => { void bridge()?.(command).then(poll, poll); };

  if (gone || !status) return null;
  // Ahead of the notice: Leave cannot work here either. The notice keeps until relaunch.
  if (status.state === "agentShell") {
    return <div style={row}>Lightbulb was opened from an agent&apos;s shell. Reopen it from the Dock to connect.</div>;
  }
  if (status.notice) {
    return (
      <div style={row} role="status">
        <span>
          Your computer joined {status.notice}&apos;s network. Teammates on it see the names, folders and status of your sessions, never your files or conversations. Hide a folder: <code>lightbulb folder &lt;path&gt; off</code>.
        </span>
        {status.leaveLimited
          ? <span>{REOPEN}</span>
          : <button type="button" style={link} onClick={() => act("network_leave")}>Leave</button>}
        <button type="button" style={link} onClick={() => act("network_notice_seen")}>Got it</button>
      </div>
    );
  }
  if (status.state === "connecting") {
    return <div style={row} role="status"><span style={dot} />Connecting your computer to {status.workspace}…</div>;
  }
  if (status.state === "optedOut" || status.state === "failed") {
    return (
      <div style={row}>
        {status.state === "failed" && <span>Couldn&apos;t connect.</span>}
        {status.connectLimited
          ? <span>{REOPEN}</span>
          : <button type="button" style={link} onClick={() => act("network_connect")}>Connect</button>}
      </div>
    );
  }
  return null;
}
