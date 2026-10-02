"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Room, RoomEvent } from "livekit-client";
import { RoomAudioRenderer, RoomContext } from "@livekit/components-react";
import { useApi } from "@/lib/api";

export const CEO_IDENTITY = "agent-ceo";

export interface HuddleError { code: string; reason?: string }

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
  join(roomId: string, roomName: string): Promise<void>;
  leave(): void;
  /** Asks Lightbulb in, like saying "Lightbulb, …". One tap: the worker's exchange ends on its own idle rules. */
  talkStart(): void;
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

  const leave = useCallback(() => {
    attempt.current++;
    const room = roomRef.current;
    roomRef.current = null;
    void room?.disconnect();
    setJoined(null); setJoining(null);
  }, []);

  const join = useCallback(async (roomId: string, roomName: string) => {
    leave();
    const mine = attempt.current;
    setError(null);
    setJoining(roomId);
    try {
      const { serverUrl, token, lightbulb } = (await api.post(`/huddles/${encodeURIComponent(roomId)}/join`, {})) as {
        serverUrl: string; token: string; lightbulb?: boolean;
      };
      if (mine !== attempt.current) return;
      const room = new Room();
      room.on(RoomEvent.DataReceived, (payload, from, _kind, topic) => {
        if (topic !== "huddle.error" || from?.identity !== CEO_IDENTITY) return; // only the worker may report
        try {
          const { code, reason } = JSON.parse(new TextDecoder().decode(payload)) as { code?: unknown; reason?: unknown };
          if (typeof code !== "string") return;
          if (code === "idle_ended") leave(); // the worker left an idle room; anyone can Go live again
          setError({ code, reason: typeof reason === "string" ? reason : undefined });
        } catch { /* not ours */ }
      });
      room.on(RoomEvent.Disconnected, () => { if (roomRef.current === room) { leave(); setError({ code: "connection_lost" }); } });
      roomRef.current = room;
      await room.connect(serverUrl, token);
      if (mine !== attempt.current) { void room.disconnect(); return; }
      await room.localParticipant.setMicrophoneEnabled(true);
      if (mine !== attempt.current) return;
      setJoined({ roomId, roomName, room, lightbulb: lightbulb !== false });
      setJoining(null);
    } catch (e) {
      if (mine !== attempt.current) return;
      const { status, body, name } = e as { status?: number; name?: string; body?: { code?: string; detail?: unknown } | null };
      const raw = body?.code ?? (typeof body?.detail === "string" ? body.detail : (body?.detail as { code?: string } | undefined)?.code);
      const code = typeof raw === "string" ? raw.toLowerCase() : undefined; // an ApiProblem says ROOM_NOT_FOUND
      leave();
      setError({ code: name === "NotAllowedError" ? "mic_denied" : name === "NotFoundError" ? "no_mic"
        : status === 503 || code === "huddles_unavailable" ? "voice_unavailable"
        : code === "room_archived" || code === "room_not_found" || code === "huddles_upstream"
          || code === "workspace_not_ready" ? code
        : "connection_lost" });
    }
  }, [api, leave]);

  // No talk_end: the worker treats it only as a nudge, and the exchange closes on its own idle rules.
  const talkStart = useCallback(() => {
    void roomRef.current?.localParticipant.performRpc({ destinationIdentity: CEO_IDENTITY, method: "talk_start", payload: "" })
      .catch(() => setError({ code: "lightbulb_unreachable" })); // e.g. the worker left between render and click
  }, []);
  const dismissError = useCallback(() => setError(null), []);

  // Leaving /chat unmounts this and disconnects.
  useEffect(() => leave, [leave]);

  const value = useMemo<Huddle>(() => ({
    roomId: joined?.roomId ?? null, roomName: joined?.roomName ?? null, lightbulb: joined?.lightbulb ?? false, joining, room: joined?.room ?? null,
    join, leave, talkStart, error, dismissError,
  }), [joined, joining, join, leave, talkStart, error, dismissError]);

  // The room's audio plays here, once, not in a header: it keeps playing while you move between rooms.
  return <HuddleContext.Provider value={value}>
    {children}
    {joined && <RoomContext.Provider value={joined.room}><RoomAudioRenderer /></RoomContext.Provider>}
  </HuddleContext.Provider>;
}

// Outside /chat's provider (tests, other surfaces) Go live renders inert instead of crashing.
const NO_HUDDLE: Huddle = {
  roomId: null, roomName: null, lightbulb: false, joining: null, room: null, join: async () => {}, leave: () => {},
  talkStart: () => {}, error: null, dismissError: () => {},
};

export function useHuddle(): Huddle {
  return useContext(HuddleContext) ?? NO_HUDDLE;
}
