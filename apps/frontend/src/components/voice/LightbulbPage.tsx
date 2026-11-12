"use client";

// The Lightbulb place (founder, 8 Nov 2026): tap to talk on top, and under it a typed chat with the assistant: the
// member's DM, or the chat in ?room= (New chat, or a chat opened from Home's "Yours"). The orb always talks in the DM.
import { Plus } from "lucide-react";
import { Button } from "@/components/ui/button";
import { TalkOrb } from "./TalkOrb";
import { LIGHTBULB, useNewChat } from "./data";
import { AssistantThread } from "./AssistantThread";
import { useAssistantDm } from "@/components/huddle/useAssistantDm";

export function LightbulbPage({ room }: { room?: string }) {
  const dm = useAssistantDm();
  const roomId = room ?? dm?.roomId;
  return <div className="vx-lightbulb">
    <div className="vx-page vx-lightbulb-top"><TalkOrb name={LIGHTBULB} /><NewChatButton /></div>
    {roomId ? <AssistantThread key={roomId} roomId={roomId} />
      : dm === undefined ? <p className="vx-empty" role="status">Loading chat…</p>
      : <p className="vx-empty">No conversation yet. Tap to talk, or start a new chat.</p>}
  </div>;
}

export function NewChatButton() {
  const { start, state } = useNewChat();
  return <Button type="button" size="sm" variant="outline" disabled={state === "busy"} onClick={() => void start()}
    title={state === "failed" ? "Couldn't start a chat. Try again." : undefined}>
    <Plus /> {state === "failed" ? "Try again" : "New chat"}</Button>;
}
