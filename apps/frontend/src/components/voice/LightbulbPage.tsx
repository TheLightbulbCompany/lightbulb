"use client";

// The Lightbulb place (founder, 8 Nov 2026): tap to talk on top, and under it the member's DM with the assistant.
import { TalkOrb } from "./TalkOrb";
import { LIGHTBULB } from "./data";
import { AssistantThread } from "./AssistantThread";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";

export function LightbulbPage() {
  const dm = useAssistantDm();
  return <div className="vx-lightbulb">
    <div className="vx-page"><TalkOrb name={LIGHTBULB} /></div>
    {dm === undefined ? <p className="vx-empty" role="status">Loading chat…</p>
      : dm ? <AssistantThread roomId={dm.roomId} />
      : <p className="vx-empty">No conversation yet. Tap to talk to start one.</p>}
  </div>;
}
