"use client";

// The Meetings page: the address a Meet invite reaches, and the follow-ups switch (moved here from the Phone page).
import { useState } from "react";
import { Check } from "@/components/voice/AgentPage";
import { nameOf, saveRefusal, useAgentConfig, useInviteAddress } from "@/components/voice/data";

export default function MeetingsPage() {
  const { data: config, error, save } = useAgentConfig();
  const invite = useInviteAddress();
  const [followUps, setFollowUps] = useState<boolean | null>(null);
  const [status, setStatus] = useState<{ text: string; err?: boolean } | null>(null);
  if (error) return <div className="vx-page"><p className="vx-empty" role="alert">Couldn&apos;t load the assistant&apos;s settings. Reload to try again.</p></div>;
  if (!config) return <div className="vx-page"><p className="vx-empty">Loading…</p></div>;
  const c = config, name = nameOf(c);
  const toggle = (next: boolean) => {
    setFollowUps(next); setStatus({ text: "Saving…" });
    save({ ...c, meetings: { ...c.meetings, followUps: next } }).then(() => { setFollowUps(null); setStatus({ text: "Saved" }); }, (e) => {
      setFollowUps(null);
      setStatus({ text: saveRefusal(e) ?? "Couldn't save the last change. Change it again to retry.", err: true });
    });
  };
  return <div className="vx-page">
    <h1 className="vx-title">Meetings</h1>
    <p className="vx-hint">Add {invite ? <span className="vx-code">{invite}</span> : "your assistant's meeting address"} as a guest on a Google Calendar event with a Google Meet link. Invites only work from people in your business. {name} joins, tells everyone it&apos;s there and that the meeting is recorded, and speaks when someone says &ldquo;{name}&rdquo;. Anyone can say &ldquo;{name}, stop transcribing me&rdquo;.</p>
    <div className="vx-grid">
      <Check label="Turn action items into follow-ups" on={followUps ?? c.meetings.followUps} set={toggle} />
    </div>
    <p className="vx-save" role="status" data-err={!!status?.err}>{status?.text}</p>
  </div>;
}
