"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { VoiceCallSession, type VoiceCallView, type VoiceTarget } from "@lightbulb/shared";
import { useApi } from "@/lib/api";
import { useLiveEvents } from "@/hooks/useLiveEvents";
import { webVoiceRtc } from "@/lib/voiceRtc";

interface VoiceCall {
  view: VoiceCallView | null;
  /** A call is connecting, live or ending: starting another is refused. */
  active: boolean;
  start(target: VoiceTarget): void;
  /** Ends a live call; on a failed one it dismisses the error. */
  hangUp(): void;
  setMuted(muted: boolean): void;
  levels(): { mic: number; out: number };
}

const VoiceCallContext = createContext<VoiceCall | null>(null);

const isActive = (v: VoiceCallView | null) => v?.state === "connecting" || v?.state === "live" || v?.state === "ending";

/** Holds the one call above the shell, so it survives room changes inside /chat; leaving /chat hangs up. */
export function VoiceCallProvider({ children }: { children: ReactNode }) {
  const api = useApi();
  const { onEvent } = useLiveEvents();
  const session = useRef<VoiceCallSession | null>(null);
  const unsubscribe = useRef<(() => void) | null>(null);
  const [view, setView] = useState<VoiceCallView | null>(null);
  const active = useRef(false); // read by start(): two clicks in one render must not open two calls
  const failed = useRef(false);

  useEffect(() => () => { void session.current?.hangUp(); unsubscribe.current?.(); }, []);

  // Stable identities: the orb's level loop depends on levels() and must not restart on every caption.
  const start = useCallback((target: VoiceTarget) => {
    if (active.current) return;
    unsubscribe.current?.();
    const s = new VoiceCallSession({ api, rtc: webVoiceRtc, onEvent });
    session.current = s;
    unsubscribe.current = s.subscribe((next) => { active.current = isActive(next); failed.current = next.state === "failed"; setView(next); });
    void s.start(target);
  }, [api, onEvent]);
  const hangUp = useCallback(() => {
    if (failed.current) { failed.current = false; setView(null); }
    else void session.current?.hangUp();
  }, []);
  const setMuted = useCallback((muted: boolean) => session.current?.setMuted(muted), []);
  const levels = useCallback(() => session.current?.levels() ?? { mic: 0, out: 0 }, []);
  const value = useMemo<VoiceCall>(() => ({ view, active: isActive(view), start, hangUp, setMuted, levels }), [view, start, hangUp, setMuted, levels]);

  return <VoiceCallContext.Provider value={value}>{children}</VoiceCallContext.Provider>;
}

// Outside /chat's provider (tests, any other surface) the Talk buttons render inert instead of crashing the page.
const NO_CALL: VoiceCall = { view: null, active: false, start: () => {}, hangUp: () => {}, setMuted: () => {}, levels: () => ({ mic: 0, out: 0 }) };

export function useVoiceCall(): VoiceCall {
  return useContext(VoiceCallContext) ?? NO_CALL;
}
