"use client";

import { useId, useState } from "react";
import Link from "next/link";
import type { SessionResumeFlow } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { ReceiptChip } from "./NativeSessionTranscript";

/**
 * Under an ended session: "Resume and send" for its owner and for whoever could send to it when it
 * ended, or where a resume someone asked for stands. It starts on the owner's Mac (plan mode for
 * anyone else), so the copy says whose Mac and what they will see there.
 */
export function SessionResume({ flow, own, ownerName }: { flow: SessionResumeFlow; own: boolean; ownerName: string }) {
  const [text, setText] = useState("");
  const id = useId();
  if (!flow.available && !flow.status) return null;
  return <div className="room-composer local-session-readonly">
    <div>
      {flow.status && <p role="status" className="text-ui text-muted-foreground">{flow.status}</p>}
      {flow.available && <form className="flex flex-col gap-2" onSubmit={event => { event.preventDefault(); void flow.resume(text).then(ok => { if (ok) setText(""); }); }}>
        <label htmlFor={id} className="sr-only">Message to send when it resumes</label>
        <textarea id={id} className="room-input" rows={2} value={text} onChange={event => setText(event.target.value)}
          placeholder="Message to send when it resumes (optional)" />
        <p className="text-caption text-muted-foreground">{own ? "Starts it again on your Mac, in the same folder."
          : `Starts it again on ${ownerName}’s Mac in plan mode. ${ownerName} sees it there and can end it.`}</p>
        <div className="local-session-actions">
          <Button type="submit" size="sm" disabled={flow.saving}>{flow.saving ? "Opening…" : own ? "Continue" : text.trim() ? "Resume and send" : "Resume"}</Button>
        </div>
      </form>}
      {flow.error && <p role="alert" className="text-ui text-warn">{flow.error}</p>}
      {flow.recovery && <Link href="/settings?panel=desktop" className="text-ui underline underline-offset-4">{flow.recovery === "folder-access" ? "Review folder access" : "Open session sharing settings"}</Link>}
    </div>
  </div>;
}

/** Where a resume's first message stands once the session runs again. */
export function ResumeDelivery({ flow }: { flow: SessionResumeFlow }) {
  return flow.chip ? <p className="text-ui">First message: <ReceiptChip chip={flow.chip} /></p> : null;
}
