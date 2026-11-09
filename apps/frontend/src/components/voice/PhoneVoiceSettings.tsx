"use client";

import { useEffect, useRef, useState } from "react";
import { PHONE_VOICES, phoneVoice, voiceSamplePath, type SampleLanguage } from "@lightbulb/shared";
import { useTalkSession } from "./TalkSessionProvider";
import type { AgentConfig } from "./data";

/** Fixed recordings from the same phone model, never a personalized greeting or a runtime generation. */
export function PhoneVoiceSettings({ c, set }: { c: Pick<AgentConfig, "voice" | "spanish">; set: (patch: Partial<AgentConfig>) => void }) {
  const talk = useTalkSession();
  const blocked = !!talk?.on;
  const [language, setLanguage] = useState<SampleLanguage>("en");
  const sampleLanguage = c.spanish ? language : "en";
  const src = voiceSamplePath(c.voice, sampleLanguage);
  return <div style={{ display: "grid", gap: 12 }}>
    <label className="vx-label">Phone voice
      <select className="vx-field" value={phoneVoice(c.voice)} onChange={(e) => set({ voice: e.target.value })}>
        {PHONE_VOICES.map((voice) => <option key={voice.id} value={voice.id}>{voice.label}</option>)}
      </select>
    </label>
    <label className="vx-check"><input type="checkbox" checked={!!c.spanish} onChange={(e) => set({ spanish: e.target.checked })} /> Also speaks Spanish</label>
    <p className="vx-hint">Opens in English. When Spanish is on, follows callers in English or Spanish. Changes apply to the next call.</p>
    <p className="vx-h">Voice sample</p>
    <p className="vx-hint">A stock AI voice sample, not your customized greeting.</p>
    {c.spanish && <label className="vx-label">Sample language
      <select className="vx-field" value={sampleLanguage} onChange={(e) => setLanguage(e.target.value as SampleLanguage)}>
        <option value="en">English</option><option value="es">Spanish</option>
      </select>
    </label>}
    {blocked ? <p className="vx-hint" role="status">End your Live conversation to play a voice sample.</p>
      : <StockSample key={src} src={src} />}
  </div>;
}

function StockSample({ src }: { src: string }) {
  const audio = useRef<HTMLAudioElement>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    const player = audio.current;
    return () => { player?.pause(); }; // source change, active Live, navigation: stop the old sample
  }, []);
  return <>
    <audio ref={audio} aria-label="Voice sample player" controls preload="none" src={src}
      onLoadStart={() => setLoading(true)} onError={() => { setLoading(false); setError(true); }}
      onCanPlay={() => { setLoading(false); setError(false); }} />
    {loading && <p className="vx-hint" role="status">Loading voice sample…</p>}
    {error && <><p className="vx-hint" role="alert">Couldn’t load this voice sample.</p>
      <button type="button" className="vx-chip" onClick={() => { setError(false); audio.current?.load(); }}>Reload voice sample</button></>}
  </>;
}
