"use client";

import useSWR from "swr";
import { Mic, PhoneOff } from "lucide-react";
import { useApi } from "@/lib/api";
import { useHuddle } from "./HuddleProvider";

interface RoomRow { roomId: string; kind: string; firstRun?: boolean; members?: { kind: string; id?: string }[] }

/** The member's DM with the assistant (the welcome DM first): Live there is owner mode, the Router by voice.
 *  undefined while loading, null when there is none. */
export function useAssistantDm(): RoomRow | null | undefined {
  const api = useApi();
  const { data } = useSWR("/messaging/rooms#voice-dm", async () => {
    const { rooms } = (await api.get("/messaging/rooms")) as { rooms: RoomRow[] };
    return rooms.find((r) => r.firstRun) ?? rooms.find((r) => r.kind === "dm" && r.members?.some((m) => m.kind === "agent")) ?? null;
  });
  return data;
}

/** The rail's one voice button: a press goes Live in the DM (the mic stays open, the assistant takes turns by
 *  itself), another ends it. The room header's strip shows the session while you are in the DM. */
export function TalkButton({ name = "Lightbulb" }: { name?: string }) {
  const huddle = useHuddle();
  const dm = useAssistantDm();
  const live = !!dm && huddle.roomId === dm.roomId;
  const joining = !!dm && huddle.joining === dm.roomId;
  const label = joining ? `Connecting to ${name}` : live ? `End talk with ${name}` : `Talk to ${name}`;
  return <button type="button" className="workspace-rail__talk" aria-label={label} title={label} aria-pressed={live}
    data-state={joining ? "connecting" : live ? "live" : undefined} disabled={!dm || joining}
    onClick={() => { if (!dm) return; if (live) huddle.leave(); else void huddle.join(dm.roomId, name); }}>
    {live ? <PhoneOff size={20} aria-hidden="true" /> : <Mic size={20} aria-hidden="true" />}
    <span>{live ? "End" : "Talk"}</span>
  </button>;
}
