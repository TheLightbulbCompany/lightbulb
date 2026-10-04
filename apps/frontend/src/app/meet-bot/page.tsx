"use client";

import { useEffect, useState } from "react";
import { Room, RoomEvent, Track, type RemoteParticipant, type RemoteTrackPublication } from "livekit-client";
import { MeetTile, useDarkTheme, type TileAgent, type TileNotice } from "./MeetTile";

// Recall's Output Media page: a public, Clerk-free LiveKit client. The ticket rides the fragment.
const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api/v1";
const BACKOFF_MS = [1000, 2000, 4000, 8000];
const MAX_ATTEMPTS = 10;
// Only the agents' voices go back into the Meet: the voice worker names their tracks `agent-<id>`, and a Meet
// person's own audio (identity `meet:…`) played back would be an echo.
const isAgentVoice = (pub: RemoteTrackPublication, p: RemoteParticipant) =>
  pub.kind === Track.Kind.Audio && !p.identity.startsWith("meet:") && pub.trackName.startsWith("agent-");
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type Agent = TileAgent;
type Attrs = Record<string, string>;
type Notice = TileNotice;

export default function MeetBotPage() {
  const [attrs, setAttrs] = useState<Attrs>({});
  const [agentNames, setAgentNames] = useState<Record<string, Agent>>({});
  const [notice, setNotice] = useState<Notice>("Connecting…");
  useDarkTheme();

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
        const { serverUrl, token, agents, mixedAudio } = (await res.json()) as { serverUrl: string; token: string; agents?: Agent[]; mixedAudio?: boolean };
        setAgentNames(Object.fromEntries((agents ?? []).map((a) => [a.id, a])));
        const r = (room = new Room());
        const gone = new Promise<"dropped">((done) => r.on(RoomEvent.Disconnected, () => done("dropped")));
        const sync = () => setAttrs(Object.assign({}, ...[...r.remoteParticipants.values()].map((p) => p.attributes)));
        // autoSubscribe is off: subscribe to the agents' voices only, as they are published.
        const listen = (pub: RemoteTrackPublication, p: RemoteParticipant) => { if (isAgentVoice(pub, p)) void pub.setSubscribed(true); };
        r.on(RoomEvent.TrackPublished, listen);
        // attach() on an element that already has an audio track REPLACES it, so each agent gets its own element
        // (detached from the DOM, which still plays) and two agents talking are both heard.
        r.on(RoomEvent.TrackSubscribed, (track, pub, p) => { if (isAgentVoice(pub, p)) document.body.appendChild(track.attach()); });
        r.on(RoomEvent.TrackUnsubscribed, (track) => { track.detach().forEach((el) => el.remove()); });
        r.on(RoomEvent.ParticipantAttributesChanged, sync).on(RoomEvent.ParticipantConnected, sync);
        await r.connect(serverUrl, token, { autoSubscribe: false });
        for (const p of r.remoteParticipants.values()) p.trackPublications.forEach((pub) => listen(pub, p));
        if (stop) { void r.disconnect(); return "dropped"; }
        await r.startAudio().catch(() => {}); // no user gesture in the bot's Chromium
        // Separate mode: the backend publishes each Meet person as their own participant, so the page sends nothing.
        if (mixedAudio !== false) {
          mic = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
          });
          await r.localParticipant.publishTrack(mic.getAudioTracks()[0], { source: Track.Source.Microphone });
        }
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

  return <MeetTile agents={agentNames} attrs={attrs} notice={notice} />;
}
