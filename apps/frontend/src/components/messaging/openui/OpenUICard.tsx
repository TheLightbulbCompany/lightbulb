"use client";

import { useCallback, useMemo, useState } from "react";
import { Renderer, type OpenUIError } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useCardContext } from "./CardContext";
import { webCardLibrary } from "./library";
import { CardPressContext } from "./renderers/shared";

const bytes = (s: string) => new TextEncoder().encode(s).length;

/**
 * One ```openui-lang fence from an agent's reply, drawn with the shared vocabulary.
 * Replies arrive whole, so nothing streams. A control's click goes through the
 * press context below: this component owns in-flight, error and sent state.
 */
export function OpenUICard({ program }: { program: string }) {
  const ctx = useCardContext();
  const tooBig = useMemo(() => bytes(program) > CARD_MAX_BYTES, [program]);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState<string | null>(null);
  const answered = ctx?.answered ?? sent;
  const press = useCallback((text: string) => {
    const reply = text.trim();
    if (!ctx?.onReply || pending || answered !== null || !reply) return;
    setPending(true);
    setError(null);
    ctx.onReply(reply).then(() => setSent(reply), () => setError("Couldn't send your reply. Try again.")).finally(() => setPending(false));
  }, [ctx, pending, answered]);
  const pressValue = useMemo(() => ({ press, disabled: !ctx?.onReply || pending || answered !== null, answered }), [press, ctx, pending, answered]);
  const onError = useCallback((errors: OpenUIError[]) => {
    // Codes only, never the program: this is the one reliability signal the spec keeps.
    if (errors.length) capture("openui.parse_error", { codes: errors.map((e) => e.code), count: errors.length });
  }, []);
  if (tooBig) return <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto my-2" style={{ background: "var(--edge)" }}>{program}</pre>;
  return (
    <div className="openui-card" data-answered={answered !== null ? "" : undefined}>
      <CardPressContext.Provider value={pressValue}>
        <Renderer response={program} library={webCardLibrary} isStreaming={false} onError={onError} />
      </CardPressContext.Provider>
      {error && <p role="alert" className="text-ui text-err">{error}</p>}
    </div>
  );
}
