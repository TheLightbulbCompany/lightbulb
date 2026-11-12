"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { Hand, PhoneOff, X } from "lucide-react";
import { Track, type Participant } from "livekit-client";
import {
  BarVisualizer, RoomContext, StartAudio, TrackToggle, useIsSpeaking, useParticipantAttributes, useParticipants, useTracks,
  type AgentState,
} from "@livekit/components-react";
import { Identity } from "@/components/voice/Identity";
import { Popover, PopoverAnchor, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { agentTint } from "@/lib/agentTint";
import { navigateShell } from "@/lib/shellNav";
import { useIsWorkspaceAdmin } from "@/hooks/useIsWorkspaceAdmin";
import {
  LISTENING_TO, agentA11yName, agentHand, agentLine, agentLiveStatus, agentWorking, floorChange, floorHolder, liveAgents, liveErrorText,
  AGENTS_JOINING, liveHappening, liveHint, nameCollides, orderAgentSlots, queuePosition, voiceFailedText, wakingText, type RoomMember,
} from "@lightbulb/shared";
import { CEO_IDENTITY, useHuddle } from "./HuddleProvider";

const HINT_KEY = "lightbulb.live.hintSessions";
const HINT_SESSIONS = 3;
const FACES = 3;
const SLOTS = 3;
const NARROW_PX = 640;
const BARS = 3;
const CHIP_MS = 6_000;
const JOIN_TIMEOUT_MS = 45_000; // the worker joins in ~20 s; past this it isn't coming
const REDUCED = "(prefers-reduced-motion: reduce)";
const subscribeReduced = (onChange: () => void) => {
  const mql = window.matchMedia(REDUCED);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
};
const useReducedMotion = () => useSyncExternalStore(subscribeReduced, () => window.matchMedia(REDUCED).matches, () => false);

type Attrs = Readonly<Record<string, string>> | undefined;
type Tile = { id: string; name: string };
type Tracks = ReturnType<typeof useTracks>;
/** Edge states per agent: the ambiguous-name chip's agent, the failed voice's agent, and whether taps are shut (voice limit/busy). */
type Edge = { ambiguous: string | null; failed: string | null; blocked: boolean };
const NO_EDGE: Edge = { ambiguous: null, failed: null, blocked: false };

/** The BarVisualizer's state for an agent's status. Reduced motion: no stepping sequence, all bars lit while speaking. */
function vizState(status: string, reduced: boolean): AgentState | undefined {
  if (reduced) return status === "speaking" ? undefined : "idle";
  if (status === "speaking") return "speaking";
  if (status === "joining") return "connecting";
  if (status === "working") return "thinking";
  return status === "listening" ? "listening" : "idle";
}

const failedLine = (edge: Edge, id: string, name: string, status: string) => (edge.failed === id && status === "idle" ? voiceFailedText(name) : null);

/** Mounted with the worker, keyed per notice: a failed voice clears once its agent is seen idle and then leaves idle.
 *  The notice and the status attribute arrive separately, so a notice that lands while the status still reads
 *  `joining` must not clear itself. */
function ClearWhenActive({ worker, failed, onClear }: { worker: Participant; failed: string; onClear: () => void }) {
  const { attributes } = useParticipantAttributes({ participant: worker });
  const status = agentLiveStatus(attributes, failed);
  const sawIdle = useRef(false);
  useEffect(() => {
    if (status === "idle") sawIdle.current = true;
    else if (sawIdle.current) onClear();
  }, [status, onClear]);
  return null;
}

/** One tile per agent: its own bars on its own `agent-<id>` track. A tap asks it in; the floor-holder's tap is a no-op. */
function AgentGroup({ tiles, attrs, tracks, slots, onTalk, ready, edge = NO_EDGE }: {
  tiles: readonly Tile[]; attrs: Attrs; tracks: Tracks; slots: number; onTalk: (id: string) => void; ready: boolean; edge?: Edge;
}) {
  const reduced = useReducedMotion();
  const byId = new Map(tiles.map((t) => [t.id, t]));
  const ordered = orderAgentSlots(tiles.map((t) => t.id), attrs);
  const floor = floorHolder(attrs);
  return (
    <span className="live-agents" role="group" aria-label="Agents in Live">
      {ordered.slice(0, slots).map((id) => {
        const name = byId.get(id)!.name;
        const status = agentLiveStatus(attrs, id);
        const extras = { working: agentWorking(attrs, id), hand: agentHand(attrs, id), listeningTo: attrs?.[LISTENING_TO], queuePos: queuePosition(attrs, id) };
        const track = reduced ? undefined : tracks.find((t) => t.publication?.trackName === `agent-${id}`);
        const pos = extras.queuePos;
        return (
          <button key={id} type="button" className="live-agent" disabled={!ready} aria-label={agentA11yName(name, status, extras)}
            title={failedLine(edge, id, name, status) ?? agentLine(name, status, extras)} data-status={status} data-speaking={status === "speaking" ? "true" : undefined}
            data-ambiguous={edge.ambiguous === id ? "true" : undefined} data-failed={edge.failed === id && status === "idle" ? "true" : undefined}
            aria-disabled={edge.blocked || undefined}
            onClick={() => { if (floor !== id && !edge.blocked) onTalk(id); }}>
            <Identity name={name} avatarOnly size="sm" tint={agentTint(id)} />
            <BarVisualizer barCount={BARS} state={vizState(status, reduced)} track={track} options={{ minHeight: 20, maxHeight: 100 }} />
            {pos != null && <span className="live-queue-badge" aria-hidden="true">{pos}</span>}
            {status === "working" && <span className="live-pip" aria-hidden="true">…</span>}
            {status === "has_news" && <span className="live-pip" aria-hidden="true"><Hand className="h-3 w-3" /></span>}
          </button>
        );
      })}
      {ordered.length > slots && <span className="live-more">+{ordered.length - slots}</span>}
    </span>
  );
}

/** The worker's attributes and tracks feed the pure group. Only mounts once the worker has joined (hooks reject undefined). */
function AgentGroupLive({ worker, tiles, slots, onTalk, edge }: { worker: Participant; tiles: readonly Tile[]; slots: number; onTalk: (id: string) => void; edge: Edge }) {
  const { attributes } = useParticipantAttributes({ participant: worker });
  const tracks = useTracks([Track.Source.Microphone]).filter((t) => t.participant.identity === CEO_IDENTITY);
  // One polite line when the floor moves; a working-only change keeps what was said (adjusting state during render).
  const ids = tiles.map((t) => t.id);
  const [snap, setSnap] = useState({ attrs: attributes as Attrs, ids, names: Object.fromEntries(tiles.map((t) => [t.id, t.name])) as Record<string, string>, text: "" });
  if (snap.attrs !== attributes || snap.ids.join() !== ids.join()) {
    const names = { ...snap.names, ...Object.fromEntries(tiles.map((t) => [t.id, t.name])) };
    setSnap({ attrs: attributes, ids, names, text: floorChange(snap.attrs, attributes, snap.ids, ids, (id) => names[id] ?? "An agent") ?? snap.text });
  }
  return (
    <>
      <AgentGroup tiles={tiles} attrs={attributes} tracks={tracks} slots={slots} onTalk={onTalk} ready edge={edge} />
      <span className="sr-only" aria-live="polite">{snap.text}</span>
    </>
  );
}

/** The popover's agent rows, what's happening and the hint line: pure over the worker's attributes. */
function AgentRows({ tiles, attrs, onTalk, hint, edge = NO_EDGE }: {
  tiles: readonly Tile[]; attrs: Attrs; onTalk: (id: string) => void; hint: string | null; edge?: Edge;
}) {
  const names = new Map(tiles.map((t) => [t.id, t.name]));
  const floor = floorHolder(attrs);
  const happening = liveHappening(attrs, (id) => names.get(id) ?? "An agent");
  return (
    <>
      {happening && <p className="live-happening">{happening}</p>}
      {tiles.length > 0 && (
        <ul className="live-agent-rows" aria-label="Agents in Live">
          {orderAgentSlots(tiles.map((t) => t.id), attrs).map((id) => {
            const status = agentLiveStatus(attrs, id);
            const extras = { working: agentWorking(attrs, id), hand: agentHand(attrs, id), listeningTo: attrs?.[LISTENING_TO], queuePos: queuePosition(attrs, id) };
            return (
              <li key={id}>
                <button type="button" className="live-row live-agent-row" disabled={!attrs} data-status={status} aria-label={agentA11yName(names.get(id)!, status, extras)}
                  data-failed={edge.failed === id && status === "idle" ? "true" : undefined} aria-disabled={edge.blocked || undefined}
                  onClick={() => { if (floor !== id && !edge.blocked) onTalk(id); }}>
                  <Identity name={names.get(id)!} avatarOnly size="sm" tint={agentTint(id)} />
                  <span className="live-row-name">{failedLine(edge, id, names.get(id)!, status) ?? agentLine(names.get(id)!, status, extras)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
      {hint && <p className="live-hint">{hint}</p>}
    </>
  );
}

function AgentRowsLive({ worker, ...rest }: { worker: Participant } & Omit<React.ComponentProps<typeof AgentRows>, "attrs">) {
  const { attributes } = useParticipantAttributes({ participant: worker });
  return <AgentRows {...rest} attrs={attributes} />;
}

/** Shown in the first few sessions only; the count rides per-viewer localStorage, which can throw. */
// Counted once per joined Live: the strip remounts on every room change, the LiveKit Room does not.
const hintCounted = new WeakSet<object>();
function useHintSessions(live: object | null) {
  const [count] = useState(() => { try { return Number(localStorage.getItem(HINT_KEY)) || 0; } catch { return 0; } });
  useEffect(() => {
    if (!live || hintCounted.has(live)) return;
    hintCounted.add(live);
    try { localStorage.setItem(HINT_KEY, String(count + 1)); } catch { /* storage unavailable */ }
  }, [live, count]);
  return count < HINT_SESSIONS;
}

function Face({ participant, stack }: { participant: Participant; stack: number }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <span className="live-face" data-speaking={speaking ? "true" : undefined} title={name} style={{ zIndex: stack }}>
      <Identity name={name} avatarOnly size="sm" />
    </span>
  );
}

function PersonRow({ participant }: { participant: Participant }) {
  const speaking = useIsSpeaking(participant);
  const name = participant.name || participant.identity;
  return (
    <li className="live-row">
      <span className="live-face" data-speaking={speaking ? "true" : undefined}><Identity name={name} avatarOnly size="sm" /></span>
      <span className="live-row-name">{name}{participant.isLocal ? " (you)" : ""}</span>
      {speaking && <span className="live-row-meta">Speaking</span>}
    </li>
  );
}

function Mic() {
  // LiveKit's own toggle: aria-pressed is "mic on"; muted means the room can't hear you.
  return <TrackToggle source={Track.Source.Microphone} className="live-icon" aria-label="Microphone" title="Microphone" />;
}

function Leave({ onLeave }: { onLeave: () => void }) {
  // Not DisconnectButton: its disconnect fires RoomEvent.Disconnected, which the provider reads as a dropped call.
  return (
    <button type="button" className="live-icon" data-tone="alert" aria-label="Leave Live" title="Leave Live" onClick={onLeave}>
      <PhoneOff className="h-4 w-4" aria-hidden="true" />
    </button>
  );
}

/** Under 640 px the header has no room for tiles: one tile, a people count. jsdom has no ResizeObserver. */
function useNarrow(ref: React.RefObject<HTMLElement | null>) {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    const header = ref.current?.closest(".room-header");
    if (!header || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver((e) => setNarrow(e[0].contentRect.width < NARROW_PX));
    ro.observe(header);
    return () => ro.disconnect();
  }, [ref]);
  return narrow;
}

function Strip({ here, members }: { here: boolean; members: readonly RoomMember[] }) {
  const { roomId, roomName, room, lightbulb, join, leave, talkStart, error, dismissError, agentNotice: notice, clearAgentNotice } = useHuddle();
  const admin = useIsWorkspaceAdmin();
  const participants = useParticipants();
  const ceo = participants.find((p) => p.identity === CEO_IDENTITY);
  const people = participants.filter((p) => p.identity !== CEO_IDENTITY);
  const stripRef = useRef<HTMLDivElement>(null);
  const narrow = useNarrow(stripRef);
  const tiles = liveAgents(members).map((m) => ({ id: m.id, name: m.displayName }));
  const humans = members.filter((m) => m.kind !== "agent");
  const hint = lightbulb ? liveHint(tiles.map((t) => ({ name: t.name, collides: nameCollides(t.name, humans) }))) : null;
  const showInline = useHintSessions(here && hint ? room : null);
  const slots = narrow ? 1 : SLOTS;
  // One notice at a time, dismissed in the provider (a tap, the 6 s chip, or the agent leaving idle) so a room change keeps it gone.
  const noticeAt = notice?.kind === "ambiguous_name" ? notice.at : null;
  useEffect(() => {
    if (noticeAt == null) return;
    const t = setTimeout(() => clearAgentNotice(noticeAt), CHIP_MS);
    return () => clearTimeout(t);
  }, [noticeAt, clearAgentNotice]);
  const clearNotice = useCallback(() => { if (notice) clearAgentNotice(notice.at); }, [notice, clearAgentNotice]);
  const blocked = error?.code === "voice_limit" || error?.code === "voice_busy";
  const edge: Edge = { ambiguous: notice?.kind === "ambiguous_name" ? notice.agentId : null, failed: notice?.kind === "voice_failed" ? notice.agentId : null, blocked };
  const tap = (id: string) => { if (notice?.agentId === id) clearNotice(); talkStart(id); };
  const chipName = tiles.find((t) => t.id === edge.ambiguous)?.name;
  const [open, setOpen] = useState(false);
  const [gaveUp, setGaveUp] = useState(false);
  useEffect(() => {
    if (ceo || !lightbulb) return; // a people-only room: Lightbulb isn't coming, so nothing to give up on
    const t = setTimeout(() => { setGaveUp(true); setOpen(true); }, JOIN_TIMEOUT_MS);
    return () => clearTimeout(t);
  }, [ceo, lightbulb]);
  // A new error opens the details so it is seen (adjusting state during render, not in an effect).
  const [shownError, setShownError] = useState(error);
  if (shownError !== error) { setShownError(error); if (error) setOpen(true); }
  const channel = roomName || "this room";
  const startAudio = <StartAudio label="Turn on sound" className="live-pill" />;

  if (!here) {
    return (
      <div className="live-strip" role="group" aria-label={`Live in ${channel}`}>
        {startAudio}
        <button type="button" className="live-summary" title={`Return to ${channel}`}
          onClick={() => window.dispatchEvent(new CustomEvent("openRoom", { detail: { roomId, threadId: null } }))}>
          <span className="live-dot" aria-hidden="true" />
          <span className="live-label">Live in {channel}</span>
          <span className="live-return">Return</span>
        </button>
        <Mic />
        <Leave onLeave={leave} />
      </div>
    );
  }

  const shown = people.slice(0, FACES);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverAnchor asChild>
        <div className="live-strip" ref={stripRef} role="group" aria-label={`Live in ${channel}`}>
          {startAudio}
          <PopoverTrigger asChild>
            <button type="button" className="live-summary" aria-label={`Live in ${channel}, ${participants.length} here. Details`}>
              <span className="live-dot" aria-hidden="true" />
              <span className="live-label">Live</span>
              {narrow
                ? <span className="live-count" aria-hidden="true">{people.length}</span>
                : <span className="live-faces" aria-hidden="true">
                    {shown.map((p, i) => <Face key={p.identity} participant={p} stack={FACES - i} />)}
                    {people.length > shown.length && <span className="live-more">+{people.length - shown.length}</span>}
                  </span>}
              {error && <span className="live-alert-dot" aria-hidden="true" />}
            </button>
          </PopoverTrigger>
          {lightbulb && tiles.length > 0 && <>
            <span className="live-divider" aria-hidden="true" />
            {ceo
              ? <AgentGroupLive worker={ceo} tiles={tiles} slots={slots} onTalk={tap} edge={edge} />
              : <AgentGroup tiles={tiles} attrs={undefined} tracks={[]} slots={slots} onTalk={tap} ready={false} edge={edge} />}
            {!ceo && !gaveUp && <span className="live-chip" role="status">{AGENTS_JOINING}</span>}
            {chipName && <span className="live-chip" role="status">Meant {chipName} the agent? Say &quot;hey {chipName}&quot; or tap</span>}
          </>}
          {ceo && edge.failed && notice && <ClearWhenActive key={notice.at} worker={ceo} failed={edge.failed} onClear={clearNotice} />}
          {hint && showInline && !narrow && <span className="live-hint-inline">{hint}</span>}
          <Mic />
          <Leave onLeave={leave} />
        </div>
      </PopoverAnchor>
      <PopoverContent align="end" className="live-popover">
        <p className="live-popover-title">Live in {channel}</p>
        {!ceo && lightbulb && gaveUp && (
          <p className="live-note" role="alert">
            {liveErrorText({ code: "agents_absent" })}
            <button type="button" className="live-pill" onClick={() => { if (roomId) void join(roomId, roomName ?? ""); }}>Retry</button>
          </p>
        )}
        {error && (
          <p className="live-note" role="alert">
            {error.code === "workspace_not_ready" ? wakingText(tiles[0]?.name ?? "Your workspace") : liveErrorText(error)}
            {admin && (error.reason === "free_minutes_used" || error.reason === "plan_inactive") && <Link href="/settings?panel=billing" className="live-pill">Plans</Link>}
            {admin && error.reason === "credits_low" && <button type="button" className="live-pill" onClick={() => navigateShell("/chat?panel=llm")}>Add credits</button>}
            <button type="button" className="live-pill" onClick={dismissError}>Dismiss</button>
          </p>
        )}
        {lightbulb && (ceo
          ? <AgentRowsLive worker={ceo} tiles={tiles} onTalk={tap} hint={hint} edge={edge} />
          : <AgentRows tiles={tiles} attrs={undefined} onTalk={tap} hint={hint} edge={edge} />)}
        <ul className="live-people" aria-label="People in Live">
          {people.map((p) => <PersonRow key={p.identity} participant={p} />)}
        </ul>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Live in the room header: the full strip in the Live room's own header, a compact "Live in #x — Return" in any
 * other, so it can always be found and left. A join that failed before a room existed shows its error instead.
 */
export function LiveStrip({ roomId, members = [] }: { roomId: string | undefined; members?: readonly RoomMember[] }) {
  const { room, roomId: liveRoomId, error, dismissError } = useHuddle();
  if (!room) {
    return error ? (
      <span className="live-note" role="alert">
        {liveErrorText(error)}
        <button type="button" className="live-icon" aria-label="Dismiss" onClick={dismissError}><X className="h-4 w-4" aria-hidden="true" /></button>
      </span>
    ) : null;
  }
  return <RoomContext.Provider value={room}><Strip here={liveRoomId === roomId} members={members} /></RoomContext.Provider>;
}
