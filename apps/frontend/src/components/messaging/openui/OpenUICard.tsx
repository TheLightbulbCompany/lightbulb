"use client";

import { useCallback, useMemo } from "react";
import { Renderer, type ActionEvent, type OpenUIError } from "@openuidev/react-lang";
import { CARD_MAX_BYTES } from "@lightbulb/shared";
import { capture } from "@/lib/analytics";
import { useCardContext } from "./CardContext";
import { webCardLibrary } from "./library";

const bytes = (s: string) => new TextEncoder().encode(s).length;

/**
 * One ```openui-lang fence from an agent's reply, drawn with the shared vocabulary.
 * Replies arrive whole, so nothing streams. A control's click reaches `onAction` as a
 * `continue_conversation` whose message is the text to post; the context posts it.
 */
export function OpenUICard({ program }: { program: string }) {
  const ctx = useCardContext();
  const tooBig = useMemo(() => bytes(program) > CARD_MAX_BYTES, [program]);
  const onAction = useCallback((event: ActionEvent) => {
    if (event.type !== "continue_conversation" || !ctx?.onReply) return;
    const text = event.humanFriendlyMessage.trim();
    if (text) void ctx.onReply(text);
  }, [ctx]);
  const onError = useCallback((errors: OpenUIError[]) => {
    // Codes only, never the program: this is the one reliability signal the spec keeps.
    if (errors.length) capture("openui.parse_error", { codes: errors.map((e) => e.code), count: errors.length });
  }, []);
  if (tooBig) return <pre className="rounded-sm p-2 text-caption font-mono overflow-x-auto my-2" style={{ background: "var(--edge)" }}>{program}</pre>;
  return (
    <div className="openui-card" data-answered={ctx?.answered !== null && ctx?.answered !== undefined ? "" : undefined}>
      <Renderer response={program} library={webCardLibrary} isStreaming={false} onAction={onAction} onError={onError} />
    </div>
  );
}
