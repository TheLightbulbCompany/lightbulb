"use client";

import { useEffect, useRef, useState } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { agentHand, agentLiveStatus, agentStateLine, agentWorking, floorHolder, meetVoiceLimitText, orderAgentSlots, voiceLimit } from "@lightbulb/shared";
import { Identity } from "@/components/teams/shared/components/Identity";
import { agentTint } from "@/lib/agentTint";

// Recall's Output Media page: a public, Clerk-free LiveKit client. The ticket rides the fragment.
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const BACKOFF_MS = [1000, 2000, 4000, 8000];
const MAX_ATTEMPTS = 10;
const STATUS_RE = /^lightbulb\.agent\.(.+)\.status$/;
const MAX_CHIPS = 6; // the tile is video in a small Meet cell: past this the type would shrink
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Agent = { id: string; name: string; avatarUrl?: string | null };
type Attrs = Record<string, string>;
type Notice = "Connecting…" | "Reconnecting…" | "This link has expired." | "Couldn't reach the agents." | null;

export default function MeetBotPage() {
  const audioEl = useRef<HTMLAudioElement>(null);
  const [attrs, setAttrs] = useState<Attrs>({});
  const [agentNames, setAgentNames] = useState<Record<string, Agent>>({});
  const [notice, setNotice] = useState<Notice>("Connecting…");

  useEffect(() => {
    let stop = false;
    let room: Room | null = null;
    const ticket = new URLSearchParams(window.location.hash.slice(1)).get("t");

    // One whole sequence: exchange the ticket, connect, publish the meeting audio. Resolves when the room drops.
    async function session(onUp: () => void): Promise<"expired" | "dropped"> {
      let mic: MediaStream | null = null;
      try {
        const res = await fetch(`${API}/meet-bot/session`, {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ticket }),
        });
        if (res.status === 401) return "expired";
        if (!res.ok) throw new Error(`session ${res.status}`);
        if (stop) return "dropped";
        const { serverUrl, token, agents } = (await res.json()) as { serverUrl: string; token: string; agents?: Agent[] };
        setAgentNames(Object.fromEntries((agents ?? []).map((a) => [a.id, a])));
        const r = (room = new Room());
        const gone = new Promise<"dropped">((done) => r.on(RoomEvent.Disconnected, () => done("dropped")));
        const sync = () => setAttrs(Object.assign({}, ...[...r.remoteParticipants.values()].map((p) => p.attributes)));
        r.on(RoomEvent.TrackSubscribed, (track) => {
          if (track.kind === Track.Kind.Audio && audioEl.current) track.attach(audioEl.current);
        });
        r.on(RoomEvent.TrackUnsubscribed, (track) => { track.detach(); });
        r.on(RoomEvent.ParticipantAttributesChanged, sync).on(RoomEvent.ParticipantConnected, sync);
        await r.connect(serverUrl, token);
        if (stop) { void r.disconnect(); return "dropped"; }
        await r.startAudio().catch(() => {}); // no user gesture in the bot's Chromium
        mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
        });
        await r.localParticipant.publishTrack(mic.getAudioTracks()[0], { source: Track.Source.Microphone });
        sync();
        setNotice(null);
        onUp();
        return await gone;
      } finally {
        mic?.getTracks().forEach((t) => t.stop());
      }
    }

    (async () => {
      if (!ticket) return setNotice("This link has expired.");
      let fails = 0;
      while (fails < MAX_ATTEMPTS && !stop) {
        try {
          if ((await session(() => { fails = 0; })) === "expired") return setNotice("This link has expired.");
        } catch { fails++; }
        if (stop) return;
        setNotice("Reconnecting…");
        // `room` is assigned inside session(); TS narrows the closure-captured let to null here.
        void (room as Room | null)?.disconnect();
        await sleep(BACKOFF_MS[Math.min(fails, BACKOFF_MS.length) - 1] ?? BACKOFF_MS[0]);
      }
      if (!stop) setNotice("Couldn't reach the agents.");
    })();

    return () => { stop = true; void room?.disconnect(); };
  }, []);

  const ids = Object.keys(attrs).map((k) => STATUS_RE.exec(k)?.[1]).filter((id): id is string => !!id);
  const nameOf = (id: string) => agentNames[id]?.name ?? (attrs[`lightbulb.agent.${id}.name`] || id);
  const ordered = orderAgentSlots(ids, attrs);
  const limit = voiceLimit(attrs);
  const floor = floorHolder(attrs);
  const speaker = ordered.find((id) => agentLiveStatus(attrs, id) === "speaking" && (!floor || floor === id)) ?? ordered.find((id) => agentLiveStatus(attrs, id) === "speaking");
  const chips = speaker ? [speaker, ...ordered.filter((id) => id !== speaker)] : ordered;
  // Only these states say more than the name; nobody can tap in a Meet, so the words are the meeting ones.
  const chipLine = (id: string) => {
    const status = agentLiveStatus(attrs, id);
    return status === "speaking" || status === "working" || status === "has_news"
      ? agentStateLine(status, { working: agentWorking(attrs, id), hand: agentHand(attrs, id), meeting: true })
      : "";
  };

  return (
    <div data-chat-theme="dark" className="relative flex flex-col items-center justify-center overflow-hidden"
      style={{ width: 1280, height: 720, background: "var(--plane-pane)", color: "var(--fg)" }}>
      <audio ref={audioEl} autoPlay />
      <span className="absolute left-6 top-4 text-sm" style={{ color: "var(--fg-mute)" }}>Lightbulb</span>
      {notice ? (
        <p className="text-4xl" role="status">{notice}</p>
      ) : chips.length > 0 ? (
        <div className="flex flex-col items-center gap-8 px-16">
          <ul className="flex flex-wrap items-center justify-center gap-5">
            {chips.slice(0, MAX_CHIPS).map((id) => {
              const line = chipLine(id);
              const live = id === speaker;
              return (
                <li key={id} className={`flex items-center gap-4 rounded-full px-6 py-3 ${live ? "text-5xl font-semibold" : "text-3xl"}`}
                  style={{ background: "var(--plane-raised)", border: `3px solid ${live ? "var(--live)" : "var(--plane-edge)"}`, color: "var(--fg)" }}>
                  <Identity name={nameOf(id)} avatarUrl={agentNames[id]?.avatarUrl} avatarOnly tint={agentTint(id)} className={live ? "[&>span]:!size-20 [&>span]:!text-3xl" : "[&>span]:!size-12 [&>span]:!text-xl"} />
                  <span>{nameOf(id)}{line && <span style={{ color: "var(--fg-soft)" }} className="font-normal">{" · "}<span>{line}</span></span>}</span>
                </li>
              );
            })}
            {chips.length > MAX_CHIPS && <li className="text-3xl" style={{ color: "var(--fg-soft)" }}>+{chips.length - MAX_CHIPS} more</li>}
          </ul>
          {limit ? (
            <p className="text-2xl" role="status" style={{ color: "var(--alert)" }}>{meetVoiceLimitText(limit)}</p>
          ) : (
            <p className="text-2xl" style={{ color: "var(--fg-soft)" }}>Say an agent&apos;s name to ask</p>
          )}
        </div>
      ) : null}
    </div>
  );
}
