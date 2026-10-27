"use client";

import { useState } from "react";

import { capture } from "@/lib/analytics";

export type CopyLineKind = "curl" | "brew" | "npm" | "agent_line";

/**
 * A command (or the paste-to-your-agent line) with a Copy button. The event
 * carries only where and which kind; never the text.
 */
export function CopyLine({
  text,
  copy = text,
  kind,
  surface,
}: {
  text: string;
  copy?: string;
  kind: CopyLineKind;
  surface: "hero" | "network";
}) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const onCopy = async () => {
    capture("install_line_copied", { surface, kind });
    try {
      await navigator.clipboard.writeText(copy);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 1500);
  };
  return (
    <div className="relative">
      <pre className="ld-mono overflow-x-auto rounded-lg border border-[var(--midnight-muted)] p-4 pr-20 text-sm leading-6 text-[var(--midnight)]">
        {text}
      </pre>
      <button
        type="button"
        onClick={onCopy}
        aria-label={`Copy the ${kind === "agent_line" ? "line for your agent" : kind + " command"}`}
        className="ld-mono absolute right-2 top-2 rounded-md border border-[var(--midnight-muted)] bg-[var(--ld-bg)] px-2 py-1 text-xs text-[var(--midnight)]"
      >
        {state === "copied" ? "Copied" : state === "failed" ? "Select and copy" : "Copy"}
      </button>
    </div>
  );
}
