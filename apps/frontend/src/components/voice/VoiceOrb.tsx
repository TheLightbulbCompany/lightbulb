"use client";

import { useCallback, useEffect, useId, useState, useSyncExternalStore } from "react";
import { Mic, MicOff, PhoneOff } from "lucide-react";
import { Identity } from "@/components/teams/shared/components/Identity";
import { agentTint } from "@/lib/agentTint";
import { AudioVisualizerRadial } from "./AudioVisualizerRadial";
import { useVoiceCall } from "./VoiceCallProvider";

const ERRORS: Record<string, string> = {
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
  no_agents: "This channel has no agents to talk to.",
};

const BARS = 24;
const SPEAKING = 0.15;
const SPEAKING_HOLD_MS = 400; // gaps between syllables must not flip the status (and its live region)
const REDUCED = "(prefers-reduced-motion: reduce)";
const subscribeReduced = (onChange: () => void) => {
  const mql = window.matchMedia(REDUCED);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
};
const reducedSnapshot = () => window.matchMedia(REDUCED).matches;

export function VoiceOrb() {
  const { view, levels, bands, hangUp, setMuted } = useVoiceCall();
  const [speaking, setSpeaking] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const reduced = useSyncExternalStore(subscribeReduced, reducedSnapshot, () => false);
  const panelId = useId();
  const live = view?.state === "live";

  // Levels are read per frame; React only re-renders when "speaking" flips. The visualizer runs its own frame loop.
  useEffect(() => {
    if (!live) return;
    let loudAt = -Infinity;
    const read = () => {
      const { out } = levels();
      const t = Date.now();
      if (out > SPEAKING) loudAt = t;
      setSpeaking(t - loudAt < SPEAKING_HOLD_MS);
    };
    if (reduced) { // no motion: the status line still follows the voice, a few times a second
      const t = setInterval(read, 250);
      return () => clearInterval(t);
    }
    let id = 0;
    const loop = () => { read(); id = requestAnimationFrame(loop); };
    id = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(id);
  }, [live, reduced, levels]);

  // The agent's voice while it speaks, yours otherwise.
  const vizBands = useCallback(() => { const b = bands(BARS); return speaking ? b.out : b.mic; }, [bands, speaking]);

  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setExpanded(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded]);

  useEffect(() => {
    if (!live || !expanded) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [live, expanded]);

  if (!view || view.state === "ended") return null;

  const nameOf = (id: string) => view.team.find((t) => t.id === id)?.name ?? view.voiceName ?? "The team";
  const failed = view.state === "failed";
  const error = failed ? (ERRORS[view.error ?? ""] ?? "The call couldn't start.") : null;
  const status = failed ? "Call ended"
    : view.state === "connecting" ? "Connecting…"
    : view.state === "ending" ? "Ending…"
    : live && speaking ? "Speaking"
    : view.working[0] ? `${nameOf(view.working[0].agentId)} is working`
    : view.pending > 0 ? "Thinking…"
    : "Listening…";
  const mood = failed ? "failed" : live && speaking ? "speaking" : view.working.length ? "working" : view.pending > 0 ? "thinking" : "listening";
  const viz = view.state === "connecting" || view.state === "ending" ? "connecting"
    : mood === "speaking" ? "speaking" : mood === "working" || mood === "thinking" ? "thinking" : "listening";
  const ring = reduced || failed; // a static ring: no motion, or nothing left to animate
  const voiceId = view.voiceAgentId ?? view.team[0]?.id;
  const working = new Set(view.working.map((w) => w.agentId));
  const left = view.deadline ? Math.max(0, Math.round((view.deadline - now) / 1000)) : null;
  const n = view.team.length;

  return (
    <div className="voice-orb" data-motion={reduced ? "reduced" : "full"} data-mood={mood} role="region" aria-label="Voice call panel">
      {(expanded || failed) && (
        <div className="voice-orb-panel" id={panelId}>
          {view.captions.map((c, i) => (
            <p key={i} className="voice-orb-caption" data-role={c.role}>
              <span className="voice-orb-caption-who">{c.role === "you" ? "You" : view.voiceName}</span>
              <span>{c.text}</span>
            </p>
          ))}
          {view.working.length > 0 && (
            <div className="voice-orb-chips">
              {view.working.map((w) => <span key={w.agentId} className="voice-orb-chip">{`${nameOf(w.agentId)} — ${w.label}`}</span>)}
            </div>
          )}
          <div className="voice-orb-controls">
            {live && left !== null && <span className="voice-orb-left">{`${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")} left`}</span>}
            {failed ? (
              <>
                <span className="voice-orb-error" role="alert">{error}</span>
                <button type="button" className="voice-orb-button" onClick={hangUp}>Close</button>
              </>
            ) : (
              <>
                <button type="button" className="voice-orb-button" aria-label="Mute" aria-pressed={view.muted} onClick={() => setMuted(!view.muted)}>
                  {view.muted ? <MicOff className="h-4 w-4" /> : <Mic className="h-4 w-4" />}
                </button>
                <button type="button" className="voice-orb-button" data-tone="alert" aria-label="Hang up" onClick={hangUp}>
                  <PhoneOff className="h-4 w-4" />
                </button>
              </>
            )}
          </div>
        </div>
      )}
      <div className="voice-orb-dock">
        <span className="voice-orb-status" aria-live="polite">{status}</span>
        <button type="button" className="voice-orb-toggle" aria-label="Voice call" aria-expanded={expanded || failed} aria-controls={panelId} onClick={() => { setNow(Date.now()); setExpanded((e) => !e); }}>
          <span className="voice-orb-core" data-viz={ring ? "ring" : "radial"}>
            {!ring && <AudioVisualizerRadial state={viz} bands={vizBands} barCount={BARS} />}
            {view.voiceName && <Identity name={view.voiceName} avatarOnly size="lg" tint={voiceId ? agentTint(voiceId) : null} />}
          </span>
          {view.kind === "room" && view.team.map((t, i) => {
            const deg = (i * 360) / n;
            return (
              <span key={t.id} className="voice-orb-ring-avatar" data-working={working.has(t.id) ? "true" : undefined}
                style={{ transform: `rotate(${deg}deg) translateY(calc(-1 * var(--voice-orb-r))) rotate(${-deg}deg)` }}>
                <Identity name={t.name} avatarOnly size="xs" tint={agentTint(t.id)} />
              </span>
            );
          })}
        </button>
      </div>
    </div>
  );
}
