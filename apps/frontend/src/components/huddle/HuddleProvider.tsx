"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Room, RoomEvent } from "livekit-client";
import { RoomAudioRenderer, RoomContext } from "@livekit/components-react";
import { LIVE_AGENT_IDENTITY, LIVE_NOTICE_TOPIC, liveJoinErrorCode, parseLiveNotice, renamedText, voiceNoteText } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { notifyFailure, notifySuccess } from "@/lib/notify";
import { navigateShell } from "@/lib/shellNav";
import { agentInspectorUrl } from "@/components/teams/shared/lib/agentInspectorUrl";

export const CEO_IDENTITY = LIVE_AGENT_IDENTITY;

export interface HuddleError { code: string; reason?: string }
export interface AgentNotice { kind: "ambiguous_name" | "voice_failed"; agentId: string; at: number }

interface Huddle {
  roomId: string | null;
  /** How the strip names the room: "#general" for a channel, the person's or group's name otherwise. */
  roomName: string | null;
  /** Lightbulb joins this room (it has a listening agent). False: a people-only voice room, no worker. */
  lightbulb: boolean;
  /** The room a join is in flight for (Go live disabled meanwhile), else null. */
  joining: string | null;
  /** The connected LiveKit room, for the header strip's LiveKit components (the mic is their TrackToggle). */
  room: Room | null;
  /** `mic` false: connect without opening or publishing the microphone (the orb's pre-connect). */
  join(roomId: string, roomName: string, mic?: boolean): Promise<void>;
  leave(): void;
  /** Asks an agent in (no id: the last one you spoke to, else the CEO), like saying its name. One tap: the worker's
   *  exchange ends on its own idle rules. */
  talkStart(agentId?: string): void;
  /** The latest speaker-only or all-hands worker notice about an agent; cleared on leave. */
  agentNotice: AgentNotice | null;
  /** Dismisses that notice (by its `at`), so it stays dismissed across room changes. */
  clearAgentNotice(at: number): void;
  /** A worker `huddle.error` code (+ reason for voice_limit; idle_ended when the worker left an idle room), or one
   *  of ours: voice_unavailable, workspace_not_ready, connection_lost, mic_denied, no_mic,
   *  lightbulb_unreachable (a Talk RPC failed). */
  error: HuddleError | null;
  dismissError(): void;
}

const HuddleContext = createContext<Huddle | null>(null);

/** Holds the one huddle above the shell, so it survives room changes inside /chat. */
export function HuddleProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const roomRef = useRef<Room | null>(null);
  const attempt = useRef(0); // a leave (or a newer join) during a connect supersedes it
  const [joined, setJoined] = useState<{ roomId: string; roomName: string; room: Room; lightbulb: boolean } | null>(null);
  const [joining, setJoining] = useState<string | null>(null);
  const [error, setError] = useState<HuddleError | null>(null);
  const [agentNotice, setAgentNotice] = useState<AgentNotice | null>(null);

  const leave = useCallback(() => {
    attempt.current++;
    const room = roomRef.current;
    roomRef.current = null;
    void room?.disconnect();
    setJoined(null); setJoining(null); setAgentNotice(null);
  }, []);

  const join = useCallback(async (roomId: string, roomName: string, mic = true) => {
    leave();
    const mine = attempt.current;
    setError(null);
    setJoining(roomId);
    try {
      const { serverUrl, token, lightbulb } = (await api.post(`/huddles/${encodeURIComponent(roomId)}/join`, {})) as {
        serverUrl: string; token: string; lightbulb?: boolean;
      };
      if (mine !== attempt.current) return;
      // stopMicTrackOnMute: a muted mic stops its capture track, so after a talk ends (TalkOrb: End, Mute, quiet) the
      // warm room keeps no live capture and the OS microphone indicator goes off.
      const room = new Room({ publishDefaults: { stopMicTrackOnMute: true } });
      room.on(RoomEvent.DataReceived, (payload, from, _kind, topic) => {
        if (topic === LIVE_NOTICE_TOPIC) {
          if (from?.identity !== CEO_IDENTITY) return; // only the worker may announce
          const n = parseLiveNotice(new TextDecoder().decode(payload));
          if (!n) return;
          if (n.kind === "ambiguous_name" || n.kind === "voice_failed") setAgentNotice({ kind: n.kind, agentId: n.agentId, at: Date.now() });
          else if (n.kind === "renamed") {
            const mine = n.by === room.localParticipant.identity;
            notifySuccess(renamedText(n.from, n.to), {
              dedupeKey: `live-rename-${n.agentId}`,
              description: n.collides ? `Say 'hey ${n.to}' in Live.` : undefined,
              ...(mine && {
                durationMs: 10_000,
                action: {
                  label: "Undo",
                  onClick: () => { void api.patch(`/teams/agents/${encodeURIComponent(n.agentId)}`, { name: n.from }).catch(() => notifyFailure("Couldn't undo the rename.")); },
                },
              }),
            });
          } else if (n.kind === "voice_note_saved") {
            notifySuccess(voiceNoteText(n.name, n.text), { action: { label: "Edit", onClick: () => navigateShell(agentInspectorUrl(n.agentId)) } });
          }
          return;
        }
        if (topic !== "huddle.error" || from?.identity !== CEO_IDENTITY) return; // only the worker may report
        try {
          const { code, reason } = JSON.parse(new TextDecoder().decode(payload)) as { code?: unknown; reason?: unknown };
          if (typeof code !== "string") return;
          if (code === "idle_ended") leave(); // the worker left an idle room; anyone can Go live again
          if (code === "quiet_ended") void room.localParticipant.setMicrophoneEnabled(false); // the talk went quiet: mic off, room warm
          setError({ code, reason: typeof reason === "string" ? reason : undefined });
        } catch { /* not ours */ }
      });
      room.on(RoomEvent.Disconnected, () => { if (roomRef.current === room) { leave(); setError({ code: "connection_lost" }); } });
      roomRef.current = room;
      await room.connect(serverUrl, token);
      if (mine !== attempt.current) { void room.disconnect(); return; }
      if (mic) await room.localParticipant.setMicrophoneEnabled(true);
      if (mine !== attempt.current) return;
      setJoined({ roomId, roomName, room, lightbulb: lightbulb !== false });
      setJoining(null);
    } catch (e) {
      if (mine !== attempt.current) return;
      leave();
      setError({ code: liveJoinErrorCode(e) });
    }
  }, [api, leave]);

  // No talk_end: the worker treats it only as a nudge, and the exchange closes on its own idle rules.
  const talkStart = useCallback((agentId?: string) => {
    void roomRef.current?.localParticipant.performRpc({ destinationIdentity: CEO_IDENTITY, method: "talk_start", payload: agentId ? JSON.stringify({ agentId }) : "" })
      .catch(() => setError({ code: "lightbulb_unreachable" })); // e.g. the worker left between render and click
  }, []);
  const dismissError = useCallback(() => setError(null), []);
  const clearAgentNotice = useCallback((at: number) => setAgentNotice((n) => (n?.at === at ? null : n)), []);

  // Leaving /chat unmounts this and disconnects.
  useEffect(() => leave, [leave]);

  const value = useMemo<Huddle>(() => ({
    roomId: joined?.roomId ?? null, roomName: joined?.roomName ?? null, lightbulb: joined?.lightbulb ?? false, joining, room: joined?.room ?? null,
    join, leave, talkStart, agentNotice, clearAgentNotice, error, dismissError,
  }), [joined, joining, join, leave, talkStart, agentNotice, clearAgentNotice, error, dismissError]);

  // The room's audio plays here, once, not in a header: it keeps playing while you move between rooms.
  return <HuddleContext.Provider value={value}>
    {children}
    {joined && <RoomContext.Provider value={joined.room}><RoomAudioRenderer /></RoomContext.Provider>}
  </HuddleContext.Provider>;
}

// Outside /chat's provider (tests, other surfaces) Go live renders inert instead of crashing.
const NO_HUDDLE: Huddle = {
  roomId: null, roomName: null, lightbulb: false, joining: null, room: null, join: async () => {}, leave: () => {},
  talkStart: () => {}, agentNotice: null, clearAgentNotice: () => {}, error: null, dismissError: () => {},
};

export function useHuddle(): Huddle {
  return useContext(HuddleContext) ?? NO_HUDDLE;
}
