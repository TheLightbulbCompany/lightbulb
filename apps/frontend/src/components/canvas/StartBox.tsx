"use client";

// Canvas's "What should we build?": one box, a few starting ideas, Enter to send. The app opens at once in
// its own conversation, where the builder asks anything it needs and posts the preview.
import { useState } from "react";
import { ArrowUp } from "lucide-react";
import { Problem } from "./appText";

const IDEAS = [
  "A sign-up form for our next workshop",
  "A booking page for 30-minute calls",
  "A tracker for customer pilots, with a stage for each",
];

export function StartBox({ onStart, onOpen, first }: {
  onStart: (request: string) => Promise<{ app_id: string }>; onOpen: (appId: string) => void; first: boolean;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async () => {
    const request = text.trim();
    if (!request || busy) return;
    setBusy(true);
    setError(null);
    try {
      const { app_id } = await onStart(request);
      setText("");
      onOpen(app_id);
    } catch (err) {
      setError((err as Error)?.message ?? "");
    } finally {
      setBusy(false);
    }
  };
  return <form className="start-box" data-first={first || undefined} onSubmit={(e) => { e.preventDefault(); void send(); }}>
    <label htmlFor="start-box-input" className="start-box__title">What should we build?</label>
    <div className="start-box__field">
      <textarea id="start-box-input" rows={first ? 3 : 2} value={text} disabled={busy}
        placeholder="Describe it in your own words: who it's for and what they do with it."
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }} />
      <button type="submit" className="start-box__send" disabled={!text.trim() || busy} aria-label={busy ? "Starting…" : "Build it"}>
        <ArrowUp size={18} aria-hidden="true" />
      </button>
    </div>
    {error !== null && <Problem raw={error || null}>That didn&apos;t start. Try again in a moment.</Problem>}
    <div className="start-box__ideas" aria-label="Ideas">
      {IDEAS.map((idea) => <button key={idea} type="button" onClick={() => setText(idea)}>{idea}</button>)}
    </div>
  </form>;
}
