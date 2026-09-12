"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

import { ASK_AI_PROMPT, ASK_AI_PROVIDERS, askAiUrl, type AskAiProvider } from "./askAiPrompt";

/**
 * "Still not sure? — ask an AI about us."
 *
 * Copy left, a 3x2 grid of provider tiles right. Each tile opens the provider
 * with the prompt prefilled AND copies the prompt to the clipboard; see
 * askAiPrompt.ts for why that belt-and-braces is deliberate.
 *
 * Class prefix is `lb-askai`, NOT `lb-ask` — the latter is already the approval
 * card inside the fake workspace UI in landing-v2.css.
 */

/**
 * Vendor marks, drawn on currentColor so they invert with the theme.
 *
 * These are deliberately SIMPLE geometric evocations, not pixel-accurate
 * reproductions of each vendor's logo — at 26px an approximation of a complex
 * mark reads as a smudge, and the tile's text label is what actually
 * identifies the provider.
 */
const MARKS: Record<string, React.ReactNode> = {
  // Interlocking hexagons — evokes the knot without trying to reproduce it.
  chatgpt: (
    <>
      <path d="M12 2.7 20.1 7.35v9.3L12 21.3 3.9 16.65v-9.3Z" />
      <path d="M12 7.6 15.9 9.8v4.4L12 16.4 8.1 14.2V9.8Z" />
    </>
  ),
  // Anthropic's sunburst: radial spokes.
  claude: <path d="M12 2.8v18.4M5.5 5.5l13 13m-13 0 13-13M2.8 12h18.4" />,
  // Four-pointed sparkle.
  gemini: (
    <path d="M12 2.5c0 5.24 4.26 9.5 9.5 9.5-5.24 0-9.5 4.26-9.5 9.5 0-5.24-4.26-9.5-9.5-9.5 5.24 0 9.5-4.26 9.5-9.5Z" />
  ),
  perplexity: <path d="M12 3.4v17.2M12 8.2 5.9 3.9v6.3L3.3 12l2.6 1.8v6.3L12 15.8m0-7.6 6.1-4.3v6.3L20.7 12l-2.6 1.8v6.3L12 15.8" />,
  // A slanted blade through a bracket.
  grok: <path d="M5.4 18.6 15.2 8.8M9.8 5.4h7.1a1.6 1.6 0 0 1 1.6 1.6v7.1M8.6 18.6h5.9a4 4 0 0 0 4-4" />,
  // Two offset sheets.
  copy: (
    <path d="M9.6 9.6h8a1.3 1.3 0 0 1 1.3 1.3v8a1.3 1.3 0 0 1-1.3 1.3h-8a1.3 1.3 0 0 1-1.3-1.3v-8A1.3 1.3 0 0 1 9.6 9.6ZM6.4 15.1H5.6a1.3 1.3 0 0 1-1.3-1.3v-8a1.3 1.3 0 0 1 1.3-1.3h8a1.3 1.3 0 0 1 1.3 1.3v.8" />
  ),
};

/** Gemini's sparkle is a solid; every other mark is a stroke. */
const FILLED = new Set(["gemini"]);

function Mark({ id }: { id: string }) {
  const filled = FILLED.has(id);
  return (
    <svg
      className="lb-askai-mark"
      viewBox="0 0 24 24"
      width="26"
      height="26"
      aria-hidden="true"
      fill={filled ? "currentColor" : "none"}
      stroke={filled ? "none" : "currentColor"}
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {MARKS[id]}
    </svg>
  );
}

export function AskAI() {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);

  /**
   * Best-effort: a denied clipboard permission must never stop the navigation,
   * so this swallows the rejection rather than surfacing it.
   */
  const copyPrompt = useCallback(async () => {
    try {
      await navigator.clipboard?.writeText(ASK_AI_PROMPT);
      return true;
    } catch {
      return false;
    }
  }, []);

  const onTileClick = useCallback(() => {
    void copyPrompt();
  }, [copyPrompt]);

  const onCopyClick = useCallback(async () => {
    const ok = await copyPrompt();
    if (!ok) return;
    setCopied(true);
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 2000);
  }, [copyPrompt]);

  return (
    <section className="lb-askai" id="ask-ai">
      <div className="lb-askai-in">
        <div className="lb-askai-copy">
          <p className="lb-eyebrow">Ask AI</p>
          <h2 className="lb-h2">Still not sure?</h2>
          <p className="lb-askai-sub">
            Don&rsquo;t just take our word for it. Ask your favourite AI what
            Lightbulb is and how it stacks up — the question is already written.
          </p>
        </div>

        <div className="lb-askai-right">
          <div className="lb-askai-grid">
            {ASK_AI_PROVIDERS.map((p: AskAiProvider) => (
              <a
                key={p.id}
                className="lb-askai-tile"
                href={askAiUrl(p)}
                target="_blank"
                rel="noopener noreferrer"
                onClick={onTileClick}
              >
                <Mark id={p.id} />
                <span>{p.label}</span>
              </a>
            ))}

            <button type="button" className="lb-askai-tile" onClick={onCopyClick}>
              <Mark id="copy" />
              <span aria-live="polite">{copied ? "Copied" : "Copy prompt"}</span>
            </button>
          </div>

          <p className="lb-askai-human">
            <Link href="/support">Talk to a human</Link>
          </p>
        </div>
      </div>
    </section>
  );
}

export default AskAI;
