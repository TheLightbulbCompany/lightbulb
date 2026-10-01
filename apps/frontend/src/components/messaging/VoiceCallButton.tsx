"use client";

import { useEffect, useRef, useState } from "react";
import { Phone, PhoneOff } from "lucide-react";
import { VoiceCallSession, type VoiceCallView } from "@lightbulb/shared";
import { Button } from "@/components/ui/button";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { webVoiceRtc } from "@/lib/voiceRtc";

const ERRORS: Record<string, string> = {
  free_minutes_used: "You've used today's free call minutes.",
  credits_low: "Add credits to keep calling.",
  plan_inactive: "Calls need an active plan.",
  voice_unavailable: "Voice calls aren't switched on yet.",
  connection_lost: "The call dropped.",
  voice_busy: "Someone in your workspace is already on a free call.",
};

export function VoiceCallButton({ agentId, agentName }: { agentId: string; agentName: string }) {
  const api = useApi();
  const { onEvent } = useLiveEvents();
  const session = useRef<VoiceCallSession | null>(null);
  const [view, setView] = useState<VoiceCallView | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => () => void session.current?.hangUp(), []);
  useEffect(() => {
    if (view?.state !== "live") return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [view?.state]);

  const start = () => {
    const s = new VoiceCallSession({ api, rtc: webVoiceRtc, onEvent });
    session.current = s;
    s.subscribe((next) => {
      setView(next);
      if (next.state === "live") setNow(Date.now());
    });
    void s.start({ agentId, agentName });
  };

  const active = view && (view.state === "connecting" || view.state === "live" || view.state === "ending");
  const left = view?.deadline ? Math.max(0, Math.round((view.deadline - now) / 1000)) : null;

  return (
    <>
      <Button type="button" variant="ghost" size="icon" aria-label={`Call ${agentName}`} disabled={!!active} onClick={start}>
        <Phone className="h-4 w-4" />
      </Button>
      {view && view.state !== "ended" && (
        <div className="voice-call-panel" role="dialog" aria-label={`Call with ${agentName}`}>
          <div className="voice-call-title">{agentName}</div>
          <div className="voice-call-status" aria-live="polite">
            {view.state === "connecting" && "Connecting…"}
            {view.state === "live" && (view.working[0] ? `${agentName} is working: ${view.working[0].label}` : "On the call")}
            {view.state === "ending" && "Ending…"}
            {view.state === "failed" && (ERRORS[view.error ?? ""] ?? "The call couldn't start.")}
          </div>
          {left !== null && view.state === "live" && <div className="voice-call-left">{Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")} left</div>}
          {view.state === "failed" ? (
            <Button type="button" variant="ghost" size="sm" onClick={() => setView(null)}>Close</Button>
          ) : (
            <Button type="button" variant="ghost" size="sm" aria-label="Hang up" onClick={() => void session.current?.hangUp()}>
              <PhoneOff className="h-4 w-4" />
            </Button>
          )}
        </div>
      )}
    </>
  );
}
