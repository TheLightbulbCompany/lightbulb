"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Room, RoomEvent } from "livekit-client";
import { useApi } from "@/lib/api";
import { useVoiceCall } from "@/components/voice/VoiceCallProvider";

export const CEO_IDENTITY = "agent-ceo";

export interface HuddleError { code: string; reason?: string }

interface Huddle {
  roomId: string | null;
  roomName: string | null;
  /** The room a join is in flight for (Go live disabled meanwhile), else null. */
  joining: string | null;
  /** The connected LiveKit room, for the bar's hooks. */
  room: Room | null;
  join(roomId: string, roomName: string): Promise<void>;
  leave(): void;
  muted: boolean;
  setMuted(muted: boolean): void;
  talking: boolean;
  talkStart(): void;
  talkEnd(): void;
  /** A worker `huddle.error` code (+ reason for voice_limit; idle_ended when the worker left an idle room), or one
   *  of ours: voice_unavailable, no_agents, workspace_not_ready, connection_lost, mic_denied, no_mic,
   *  lightbulb_unreachable (a Talk RPC failed). */
  error: HuddleError | null;
  dismissError(): void;
}

const HuddleContext = createContext<Huddle | null>(null);

/** Holds the one huddle above the shell (like the voice call), so it survives room changes inside /chat. */
export function HuddleProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const voice = useVoiceCall();
  const roomRef = useRef<Room | null>(null);
  const attempt = useRef(0); // a leave (or a newer join) during a connect supersedes it
  const talkingRef = useRef(false); // read by talkStart/End: key repeat and pointer+key must send one RPC
  const [joined, setJoined] = useState<{ roomId: string; roomName: string; room: Room } | null>(null);
  const [joining, setJoining] = useState<string | null>(null);
  const [muted, setMutedState] = useState(false);
  const [talking, setTalking] = useState(false);
  const [error, setError] = useState<HuddleError | null>(null);

  const leave = useCallback(() => {
    attempt.current++;
    const room = roomRef.current;
    roomRef.current = null;
    talkingRef.current = false;
    void room?.disconnect();
    setJoined(null); setJoining(null); setTalking(false); setMutedState(false);
  }, []);

  const join = useCallback(async (roomId: string, roomName: string) => {
    if (voice.active) return; // one at a time
    leave();
    const mine = attempt.current;
    setError(null);
    setJoining(roomId);
    try {
      const { serverUrl, token } = (await api.post(`/huddles/${encodeURIComponent(roomId)}/join`, {})) as { serverUrl: string; token: string };
      if (mine !== attempt.current) return;
      const room = new Room();
      room.on(RoomEvent.DataReceived, (payload, from, _kind, topic) => {
        if (topic !== "huddle.error" || from?.identity !== CEO_IDENTITY) return; // only the worker may report
        try {
          const { code, reason } = JSON.parse(new TextDecoder().decode(payload)) as { code?: unknown; reason?: unknown };
          if (typeof code !== "string") return;
          talkingRef.current = false; setTalking(false); // a refused summon: a tapped-on Talk turns back off
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
      setJoined({ roomId, roomName, room });
      setJoining(null);
    } catch (e) {
      if (mine !== attempt.current) return;
      const { status, body, name } = e as { status?: number; name?: string; body?: { code?: string; detail?: unknown } | null };
      const raw = body?.code ?? (typeof body?.detail === "string" ? body.detail : (body?.detail as { code?: string } | undefined)?.code);
      const code = typeof raw === "string" ? raw.toLowerCase() : undefined; // an ApiProblem says ROOM_NOT_FOUND
      leave();
      setError({ code: name === "NotAllowedError" ? "mic_denied" : name === "NotFoundError" ? "no_mic"
        : status === 503 || code === "huddles_unavailable" ? "voice_unavailable"
        : code === "no_agents" || code === "room_archived" || code === "room_not_found" || code === "huddles_upstream"
          || code === "workspace_not_ready" ? code
        : "connection_lost" });
    }
  }, [api, leave, voice.active]);

  const setMuted = useCallback((next: boolean) => {
    setMutedState(next);
    void roomRef.current?.localParticipant.setMicrophoneEnabled(!next);
  }, []);
  const rpc = useCallback((method: "talk_start" | "talk_end") => {
    void roomRef.current?.localParticipant.performRpc({ destinationIdentity: CEO_IDENTITY, method, payload: "" })
      .catch(() => {
        if (method === "talk_start") { talkingRef.current = false; setTalking(false); }
        setError({ code: "lightbulb_unreachable" }); // e.g. Talk before the worker has joined
      });
  }, []);
  const talkStart = useCallback(() => {
    if (!roomRef.current || talkingRef.current) return;
    talkingRef.current = true; setTalking(true); rpc("talk_start");
  }, [rpc]);
  const talkEnd = useCallback(() => {
    if (!talkingRef.current) return;
    talkingRef.current = false; setTalking(false); rpc("talk_end");
  }, [rpc]);
  const dismissError = useCallback(() => setError(null), []);

  // One at a time: a call starting ends the huddle. Leaving /chat unmounts this and disconnects.
  // eslint-disable-next-line react-hooks/set-state-in-effect -- reacting to another provider's state, not derivable
  useEffect(() => { if (voice.active) leave(); }, [voice.active, leave]);
  useEffect(() => leave, [leave]);

  const value = useMemo<Huddle>(() => ({
    roomId: joined?.roomId ?? null, roomName: joined?.roomName ?? null, joining, room: joined?.room ?? null,
    join, leave, muted, setMuted, talking, talkStart, talkEnd, error, dismissError,
  }), [joined, joining, join, leave, muted, setMuted, talking, talkStart, talkEnd, error, dismissError]);

  return <HuddleContext.Provider value={value}>{children}</HuddleContext.Provider>;
}

// Outside /chat's provider (tests, other surfaces) the Huddle button renders inert instead of crashing.
const NO_HUDDLE: Huddle = {
  roomId: null, roomName: null, joining: null, room: null, join: async () => {}, leave: () => {}, muted: false, setMuted: () => {},
  talking: false, talkStart: () => {}, talkEnd: () => {}, error: null, dismissError: () => {},
};

export function useHuddle(): Huddle {
  return useContext(HuddleContext) ?? NO_HUDDLE;
}
