import { useAuth } from "@clerk/clerk-expo";
import { createApiClient, LIVE_AGENT_IDENTITY, liveJoinErrorCode } from "@lightbulb/shared";
import type { Room } from "livekit-client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { makeAdapter } from "../lib/adapter";
import { setupWebRTC } from "../lib/webrtc";
import { useLiveEvents } from "./live-events";

export interface LiveError { code: string; reason?: string }

/** A room's Live on LiveKit (internally a "huddle"; never say that word to people). Any room kind. */
export interface Live {
  roomId: string | null;
  /** How the sheet names the room: "#general" for a channel, the person's or group's name otherwise. */
  roomName: string | null;
  /** Lightbulb joins this room (it has a listening agent). False: a people-only Live, no Lightbulb tile. */
  lightbulb: boolean;
  /** The room a join is in flight for (Go live disabled meanwhile), else null. */
  joining: string | null;
  /** The connected LiveKit room, for the sheet's hooks. */
  room: Room | null;
  /** The sheet is showing; Hide keeps the room and its audio. */
  open: boolean;
  setOpen(open: boolean): void;
  join(roomId: string, roomName: string): Promise<void>;
  leave(): void;
  /** The one mic toggle: muted means the room can't hear you. */
  muted: boolean;
  setMuted(muted: boolean): void;
  /** Asks Lightbulb in, like saying "Lightbulb, …": one talk_start, no hold; the worker's exchange ends on its own. */
  talkStart(): void;
  /** A worker `huddle.error` code (+ reason for voice_limit; idle_ended when the worker left an idle room), or one
   *  of ours (see liveJoinErrorCode, plus lightbulb_unreachable when a Talk RPC fails). */
  error: LiveError | null;
  dismissError(): void;
}

const LiveContext = createContext<Live | null>(null);

function decode(payload: Uint8Array): string {
  if (typeof TextDecoder !== "undefined") return new TextDecoder().decode(payload);
  return String.fromCharCode(...payload); // ponytail: ASCII only; the worker's codes and reasons are ASCII
}

/** Holds the one Live above every screen (the web's HuddleProvider), so it survives navigation. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const roomRef = useRef<Room | null>(null);
  const attempt = useRef(0); // a leave (or a newer join) during a connect supersedes it
  const [joined, setJoined] = useState<{ roomId: string; roomName: string; room: Room; lightbulb: boolean } | null>(null);
  const [joining, setJoining] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [muted, setMutedState] = useState(false);
  const [error, setError] = useState<LiveError | null>(null);

  const leave = useCallback(() => {
    attempt.current++;
    const room = roomRef.current;
    roomRef.current = null;
    void room?.disconnect();
    setJoined(null); setJoining(null); setMutedState(false); setOpen(false);
  }, []);
  // Ends Live and puts the reason in front of the person, even if they had hidden the sheet.
  const fail = useCallback((code: string) => { leave(); setError({ code }); setOpen(true); }, [leave]);

  const join = useCallback(async (roomId: string, roomName: string) => {
    leave();
    const mine = attempt.current;
    setError(null);
    setJoining(roomId);
    setOpen(true);
    try {
      setupWebRTC();
      const { serverUrl, token, lightbulb } = (await api.post(`/huddles/${encodeURIComponent(roomId)}/join`, {})) as {
        serverUrl: string; token: string; lightbulb?: boolean;
      };
      if (mine !== attempt.current) return;
      /* eslint-disable-next-line @typescript-eslint/no-require-imports */
      const { Room: LiveKitRoom, RoomEvent } = require("livekit-client") as typeof import("livekit-client");
      const room = new LiveKitRoom();
      room.on(RoomEvent.DataReceived, (payload, from, _kind, topic) => {
        if (topic !== "huddle.error" || from?.identity !== LIVE_AGENT_IDENTITY) return; // only the worker may report
        try {
          const { code, reason } = JSON.parse(decode(payload)) as { code?: unknown; reason?: unknown };
          if (typeof code !== "string") return;
          if (code === "idle_ended") { fail(code); return; } // the worker left an idle room; anyone can Go live again
          setError({ code, reason: typeof reason === "string" ? reason : undefined });
        } catch { /* not ours */ }
      });
      room.on(RoomEvent.Disconnected, () => { if (roomRef.current === room) fail("connection_lost"); });
      roomRef.current = room;
      await room.connect(serverUrl, token);
      if (mine !== attempt.current) { void room.disconnect(); return; }
      try {
        await room.localParticipant.setMicrophoneEnabled(true);
      } catch {
        throw { name: "NotAllowedError" }; // the mic was refused
      }
      if (mine !== attempt.current) return;
      setJoined({ roomId, roomName, room, lightbulb: lightbulb !== false });
      setJoining(null);
    } catch (e) {
      if (mine !== attempt.current) return;
      fail(liveJoinErrorCode(e));
    }
  }, [api, leave, fail]);

  const setMuted = useCallback((next: boolean) => {
    setMutedState(next);
    void roomRef.current?.localParticipant.setMicrophoneEnabled(!next);
  }, []);
  // No talk_end: the worker treats it only as a nudge, and the exchange closes on its own idle rules.
  const talkStart = useCallback(() => {
    void roomRef.current?.localParticipant.performRpc({ destinationIdentity: LIVE_AGENT_IDENTITY, method: "talk_start", payload: "" })
      .catch(() => setError({ code: "lightbulb_unreachable" })); // e.g. the worker left between render and tap
  }, []);
  const dismissError = useCallback(() => setError(null), []);

  // Unmounting (sign-out, workspace switch) disconnects.
  useEffect(() => leave, [leave]);

  const value = useMemo<Live>(() => ({
    roomId: joined?.roomId ?? null, roomName: joined?.roomName ?? null, lightbulb: joined?.lightbulb ?? false, joining,
    room: joined?.room ?? null, open, setOpen, join, leave, muted, setMuted, talkStart, error, dismissError,
  }), [joined, joining, open, join, leave, muted, setMuted, talkStart, error, dismissError]);

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

const NO_LIVE: Live = {
  roomId: null, roomName: null, lightbulb: false, joining: null, room: null, open: false, setOpen: () => {}, join: async () => {},
  leave: () => {}, muted: false, setMuted: () => {}, talkStart: () => {}, error: null, dismissError: () => {},
};

export function useLive(): Live {
  return useContext(LiveContext) ?? NO_LIVE;
}

const RETRY_MS = 30_000; // every open room header reads this: back off hard while LiveKit is down

/** Who is Live in a room: seeded from the server, then kept by `room.huddle.changed` (a full snapshot).
 *  Events can drop, so the seed is re-read on every reconnect; an event newer than the seed wins. */
export function useRoomLive(roomId: string | undefined): { userIds: string[]; available: boolean } {
  const { getToken } = useAuth();
  const getTokenRef = useRef(getToken);
  getTokenRef.current = getToken;
  const api = useMemo(() => createApiClient(makeAdapter(() => getTokenRef.current())), []);
  const { onEvent, connectionRevision } = useLiveEvents();
  const [state, setState] = useState<{ roomId?: string; userIds: string[] }>({ userIds: [] });
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    if (!roomId) return;
    let stale = false;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let eventSeen = false;
    const off = onEvent((event) => {
      if (event.type !== "room.huddle.changed" || event.payload.roomId !== roomId) return;
      eventSeen = true;
      setState({ roomId, userIds: event.payload.userIds });
    });
    const seed = () => {
      eventSeen = false;
      api.get(`/huddles/${encodeURIComponent(roomId)}`)
        .then((raw) => {
          if (stale) return;
          const { userIds, available: on } = raw as { userIds: string[]; available?: boolean };
          setAvailable(on === true);
          if (!eventSeen) setState({ roomId, userIds });
        })
        // A 404 is permanent (a shared-channel guest, a deleted room): stop asking.
        .catch((e: { status?: number }) => { if (!stale && e?.status !== 404) retry = setTimeout(seed, RETRY_MS); });
    };
    seed();
    return () => { stale = true; clearTimeout(retry); off(); };
  }, [api, onEvent, roomId, connectionRevision]);

  return { userIds: state.roomId === roomId ? state.userIds : [], available };
}
