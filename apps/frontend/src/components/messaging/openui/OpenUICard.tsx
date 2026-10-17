"use client";

import { useCallback, useMemo } from "react";
import { Renderer, type OpenUIError } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useCardContext } from "./CardContext";
import { webCardLibrary } from "./library";
import { CardPressContext, type CardPress } from "./renderers/shared";

const bytes = (s: string) => new TextEncoder().encode(s).length;

/**
 * One ```openui-lang fence from an agent's reply, drawn with the shared vocabulary.
 * Replies arrive whole, so nothing streams. A control's click goes through the
 * press context below, which is the MESSAGE's (CardContext.useCardPressState): two fences
 * in one reply share one answer.
 */
export function OpenUICard({ program }: { program: string }) {
  const ctx = useCardContext();
  const tooBig = useMemo(() => bytes(program) > CARD_MAX_BYTES, [program]);
  const answered = ctx?.answered ?? null;
  const pressValue = useMemo<CardPress>(() => ({
    press: ctx?.press ?? (() => {}), disabled: !ctx?.press || ctx.pending || ctx.answered !== null, answered,
  }), [ctx, answered]);
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
      {ctx?.error && <p role="alert" className="text-ui text-err">{ctx.error}</p>}
    </div>
  );
}
