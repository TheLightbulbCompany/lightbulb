"use client";

// While the member talks in this room's Live, their words show as a pending bubble in the conversation, the way a
// voice mode does (founder, 8 Nov 2026). The words come from LiveKit's transcription stream, which the voice worker
// already publishes (owner.py's realtime talker transcribes the member), the same stream TalkOrb's caption reads. The
// talker delegates the request to the room's Router as the member's message (via voice); the bubble steps aside once
// any new message of the member's is in the room, or CLEAR_MS after the line ends (a greeting the talker answered
// itself, or a post that failed), so it never sticks.
import { useEffect, useState } from "react";
import { RoomContext, useLocalParticipant, useTranscriptions } from "@livekit/components-react";
import { Identity } from "@/components/voice/Identity";
import { useHuddle } from "@/components/huddle/HuddleProvider";

export const CLEAR_MS = 4000;

type Line = { messageSeq: number; text: string; author: { kind: string; id: string } };

export function LiveTranscript({ roomId, messages, name }: { roomId: string; messages: readonly Line[]; name: string }) {
  const huddle = useHuddle();
  if (huddle.roomId !== roomId || !huddle.room) return null;
  return <RoomContext.Provider value={huddle.room}><Heard messages={messages} name={name} /></RoomContext.Provider>;
}

function Heard({ messages, name }: { messages: readonly Line[]; name: string }) {
  const { localParticipant } = useLocalParticipant();
  const me = localParticipant.identity;
  const latest = useTranscriptions({ participantIdentities: [me] }).at(-1);
  // the room's newest seq when this line began: only a message posted after it can be the line's
  const [line, setLine] = useState<{ segment: string; since: number } | null>(null);
  const [cleared, setCleared] = useState<string | null>(null); // the segment whose line ended CLEAR_MS ago
  const text = latest?.text.trim() ?? "";
  const segment = latest ? latest.streamInfo.attributes?.["lk.segment_id"] ?? latest.streamInfo.id : "";
  const final = latest?.streamInfo.attributes?.["lk.transcription_final"] === "true";
  useEffect(() => {
    if (!final) return;
    const t = setTimeout(() => setCleared(segment), CLEAR_MS);
    return () => clearTimeout(t);
  }, [final, segment]);
  if (segment && line?.segment !== segment) setLine({ segment, since: messages.reduce((n, m) => Math.max(n, m.messageSeq), 0) });
  if (!text || line?.segment !== segment || cleared === segment) return null;
  if (messages.some((m) => m.messageSeq > line.since && m.author.kind === "human" && m.author.id === me)) return null;
  return <article className="room-message opacity-60" aria-label={`${name} is speaking`}>
    <Identity name={name} avatarOnly size="default" />
    <div className="room-message-content">
      <div className="room-message-byline"><span className="font-semibold">{name}</span><span className="text-ui text-muted-foreground">Speaking…</span></div>
      <p>{text}</p>
    </div>
  </article>;
}
