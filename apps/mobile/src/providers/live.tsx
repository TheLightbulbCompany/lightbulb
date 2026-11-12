import { useAuth } from "@clerk/expo";
import {
  createApiClient, createMessagingClient, LIVE_AGENT_IDENTITY, LIVE_NOTICE_TOPIC, liveJoinErrorCode, parseLiveNotice, SPEAKING_AGENT,
  type LiveNotice, type RoomMember,
} from "@lightbulb/shared";
import type { Room } from "livekit-client";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { AppState } from "react-native";

import { makeAdapter } from "../lib/adapter";
import { setupWebRTC } from "../lib/webrtc";
import { useLiveEvents } from "./live-events";

export interface LiveError { code: string; reason?: string }

/** A worker `live.notice` with the time it arrived (the sheet and toasts show it briefly). */
export type LiveNoticeAt = LiveNotice & { at: number };

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
  /** Asks an agent in (no id: the last agent you spoke to, else the CEO): one talk_start, no hold; the worker's exchange ends on its own. */
  talkStart(agentId?: string): void;
  /** The worker's participant attributes (C4), {} until it joins. Read them with the shared `agentLiveStatus` & co. */
  workerAttrs: Readonly<Record<string, string>>;
  workerPresent: boolean;
  /** The room's members (agents and people), fetched on join and refetched on a rename, a member change and sheet open. */
  roster: RoomMember[];
  /** The last non-empty floor-holder; survives the worker clearing `speaking_agent`. */
  lastSpeaker: string | null;
  /** A `renamed` / `voice_note_saved` banner (NoticeBanner). */
  notice: LiveNoticeAt | null;
  dismissNotice(): void;
  /** An `ambiguous_name` / `voice_failed` for one tile; the Stage consumes it once (dismissAgentNotice) so it never
   *  replays on a sheet reopen and never displaces a rename's Undo. */
  agentNotice: LiveNoticeAt | null;
  dismissAgentNotice(): void;
  /** This device's LiveKit identity (to tell "by you" from "by Aden"). */
  localIdentity: string | null;
  /** A worker `huddle.error` code (+ reason for voice_limit; idle_ended when the worker left an idle room), or one
   *  of ours (see liveJoinErrorCode, plus lightbulb_unreachable when a Talk RPC fails). */
  error: LiveError | null;
  dismissError(): void;
}

const LiveContext = createContext<Live | null>(null);

const decode = (payload: Uint8Array) => new TextDecoder().decode(payload); // UTF-8: names like "Zoë" are not ASCII

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
  const [workerAttrs, setWorkerAttrs] = useState<Readonly<Record<string, string>>>({});
  const [workerPresent, setWorkerPresent] = useState(false);
  const [roster, setRoster] = useState<RoomMember[]>([]);
  const [lastSpeaker, setLastSpeaker] = useState<string | null>(null);
  const [notice, setNotice] = useState<LiveNoticeAt | null>(null);
  const [agentNotice, setAgentNotice] = useState<LiveNoticeAt | null>(null);
  const [localIdentity, setLocalIdentity] = useState<string | null>(null);
  const messaging = useMemo(() => createMessagingClient(api), [api]);
  const { onEvent } = useLiveEvents();
  const joinedRoomId = useRef<string | null>(null);

  // A failed read keeps the old roster; the next trigger tries again.
  const refetchRoster = useCallback(() => {
    const id = joinedRoomId.current;
    if (!id) return;
    void (async () => {
      try {
        const d = await messaging.getRoom(id);
        if (joinedRoomId.current === id && Array.isArray(d?.members)) setRoster(d.members);
      } catch { /* keep the old roster */ }
    })();
  }, [messaging]);

  const leave = useCallback(() => {
    attempt.current++;
    const room = roomRef.current;
    roomRef.current = null;
    void room?.disconnect();
    joinedRoomId.current = null;
    setJoined(null); setJoining(null); setMutedState(false); setOpen(false);
    setWorkerAttrs({}); setWorkerPresent(false); setRoster([]); setLastSpeaker(null); setNotice(null); setAgentNotice(null); setLocalIdentity(null);
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
      // Only the worker's attributes count: a person's own attributes never reach the agent state.
      const readWorker = (p?: { identity: string; attributes?: Record<string, string> }) => {
        if (p?.identity !== LIVE_AGENT_IDENTITY) return;
        const attrs = { ...(p.attributes ?? {}) };
        setWorkerAttrs(attrs);
        if (attrs[SPEAKING_AGENT]) setLastSpeaker(attrs[SPEAKING_AGENT]);
      };
      room.on(RoomEvent.ParticipantConnected, (p) => { if (p.identity === LIVE_AGENT_IDENTITY) { setWorkerPresent(true); readWorker(p); } });
      room.on(RoomEvent.ParticipantDisconnected, (p) => { if (p.identity === LIVE_AGENT_IDENTITY) { setWorkerPresent(false); setWorkerAttrs({}); } });
      room.on(RoomEvent.ParticipantAttributesChanged, (_changed, p) => readWorker(p));
      room.on(RoomEvent.DataReceived, (payload, from, _kind, topic) => {
        if (topic === LIVE_NOTICE_TOPIC) {
          if (from?.identity !== LIVE_AGENT_IDENTITY) return; // a person cannot forge a notice
          const n = parseLiveNotice(decode(payload));
          if (!n) return;
          const at = { ...n, at: Date.now() };
          if (n.kind === "renamed" || n.kind === "voice_note_saved") setNotice(at);
          else setAgentNotice(at);
          if (n.kind === "renamed") refetchRoster();
          return;
        }
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
      joinedRoomId.current = roomId;
      setLocalIdentity(room.localParticipant.identity ?? null);
      const worker = room.remoteParticipants?.get(LIVE_AGENT_IDENTITY); // already in the room when we connected
      if (worker) { setWorkerPresent(true); readWorker(worker); }
      refetchRoster();
      setJoined({ roomId, roomName, room, lightbulb: lightbulb !== false });
      setJoining(null);
    } catch (e) {
      if (mine !== attempt.current) return;
      fail(liveJoinErrorCode(e));
    }
  }, [api, leave, fail, refetchRoster]);

  const setMuted = useCallback((next: boolean) => {
    setMutedState(next);
    void roomRef.current?.localParticipant.setMicrophoneEnabled(!next);
  }, []);
  // No talk_end: the worker treats it only as a nudge, and the exchange closes on its own idle rules.
  const talkStart = useCallback((agentId?: string) => {
    void roomRef.current?.localParticipant.performRpc({
      destinationIdentity: LIVE_AGENT_IDENTITY, method: "talk_start", payload: typeof agentId === "string" && agentId ? JSON.stringify({ agentId }) : "",
    })
      .catch(() => setError({ code: "lightbulb_unreachable" })); // e.g. the worker left between render and tap
  }, []);
  const dismissError = useCallback(() => setError(null), []);
  const dismissNotice = useCallback(() => setNotice(null), []);
  const dismissAgentNotice = useCallback(() => setAgentNotice(null), []);
  const setOpenAndRefresh = useCallback((next: boolean) => { setOpen(next); if (next) refetchRoster(); }, [refetchRoster]);

  useEffect(() => onEvent((e) => {
    if (e.type === "room.member.updated" && e.payload.roomId === joinedRoomId.current) refetchRoster();
  }), [onEvent, refetchRoster]);

  // Unmounting (sign-out, workspace switch) disconnects.
  useEffect(() => leave, [leave]);

  const value = useMemo<Live>(() => ({
    roomId: joined?.roomId ?? null, roomName: joined?.roomName ?? null, lightbulb: joined?.lightbulb ?? false, joining,
    room: joined?.room ?? null, open, setOpen: setOpenAndRefresh, join, leave, muted, setMuted, talkStart, error, dismissError,
    workerAttrs, workerPresent, roster, lastSpeaker, notice, dismissNotice, agentNotice, dismissAgentNotice, localIdentity,
  }), [joined, joining, open, setOpenAndRefresh, join, leave, muted, setMuted, talkStart, error, dismissError,
    workerAttrs, workerPresent, roster, lastSpeaker, notice, dismissNotice, agentNotice, dismissAgentNotice, localIdentity]);

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

const NO_LIVE: Live = {
  roomId: null, roomName: null, lightbulb: false, joining: null, room: null, open: false, setOpen: () => {}, join: async () => {},
  leave: () => {}, muted: false, setMuted: () => {}, talkStart: () => {}, error: null, dismissError: () => {},
  workerAttrs: {}, workerPresent: false, roster: [], lastSpeaker: null, notice: null, dismissNotice: () => {}, agentNotice: null, dismissAgentNotice: () => {}, localIdentity: null,
};

export function useLive(): Live {
  return useContext(LiveContext) ?? NO_LIVE;
}

const RETRY_MS = 30_000; // every open room header reads this: back off hard while LiveKit is down

