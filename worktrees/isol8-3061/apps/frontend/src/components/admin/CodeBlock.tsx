import * as React from "react";

import { cn } from "@/lib/utils";

export interface CodeBlockProps {
  /** JSON object (pretty-printed) or a raw string. */
  value: object | string;
  /** Hint for highlighting. JSON gets bold-key treatment; others render plain. */
  language?: "json" | "yaml" | "text";
  /** Max visible height in pixels before scrolling. Defaults to 400. */
  maxHeight?: number;
  className?: string;
}

// Match JSON object keys at the start of a line: indent, "key", optional space, ":".
// Operates on the post-escape string so the quote chars are &quot;. Keys with
// embedded quote characters are uncommon; keep this regex simple and targeted.
const KEY_PATTERN = /^(\s*)(&quot;[^&\n]*?&quot;)(\s*:)/gm;

function escapeHtml(input: string): string {
  return input
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderJsonHighlighted(jsonText: string): string {
  // Bold object keys only. Pure HTML-string approach keeps this server-safe
  // and avoids pulling in a syntax highlighter just for "make keys bold".
  const escaped = escapeHtml(jsonText);
  return escaped.replace(
    KEY_PATTERN,
    (_match, indent: string, key: string, colon: string) =>
      `${indent}<span class="font-semibold text-info">${key}</span>${colon}`,
  );
}

/**
 * Read-only code/JSON viewer with bold-key highlighting for JSON. Used to
 * render redacted `openclaw.json` and similar config blobs on admin pages.
 */
export function CodeBlock({
  value,
  language = "json",
  maxHeight = 400,
  className,
}: CodeBlockProps) {
  const text =
    typeof value === "string" ? value : JSON.stringify(value, null, 2);

  const isJson = language === "json" && typeof value !== "string";

  return (
    <div
      className={cn(
        "overflow-auto rounded-md border border-border bg-background p-4",
        className,
      )}
      style={{ maxHeight }}
      data-language={language}
    >
      <pre className="m-0 whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground">
        {isJson ? (
          <code
            // We escape input above and only inject our own <span> tags.
            dangerouslySetInnerHTML={{ __html: renderJsonHighlighted(text) }}
          />
        ) : (
          <code>{text}</code>
        )}
      </pre>
    </div>
  );
}
