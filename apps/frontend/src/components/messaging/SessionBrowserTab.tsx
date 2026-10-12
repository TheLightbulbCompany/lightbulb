"use client";

import { useEffect } from "react";
import type { LocalSession } from "@lightbulb/shared";
import { BrowserFrameView } from "@/components/workspace/panels/BrowserFrameView";
import type { SessionFrames } from "./SessionTerminalView";

/** The Browser tab shows while a running session has an open browser connection, for people watching
 *  it from elsewhere (its owner at that Mac sees the real window). */
export function showsBrowserTab(session: Pick<LocalSession, "browser" | "status">, watchingRemotely: boolean): boolean {
  return watchingRemotely && !!session.browser && session.status !== "ended" && session.status !== "offline";
}

/** A shared session's browser (spec 2026-10-11 §3): the page the session drives, as 480 px frames on
 *  the session's frames channel while this tab is open; people who may type into the session may click
 *  and type in it. Nothing is recorded. */
export function SessionBrowserTab({ sessionId, frames, canType }: { sessionId: string; frames?: SessionFrames; canType: boolean }) {
  useEffect(() => (frames ? frames.claim(frames.channel, () => undefined) : undefined), [frames]);
  return (
    <div className="relative min-h-[320px] flex-1" role="region" aria-label="Session browser">
      <BrowserFrameView session={{ sessionId, canType }}
        empty={<p role="status" className="text-sm text-[var(--fg-mute)]">Waiting for the session&apos;s page…</p>} />
    </div>
  );
}
