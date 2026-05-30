"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";

function safeHref(href?: string): string | undefined {
  if (!href) return undefined;
  const v = href.trim().toLowerCase();
  if (v.startsWith("javascript:") || v.startsWith("data:")) return undefined;
  return href;
}

// react-markdown does NOT render raw HTML (no rehype-raw), so the body is
// inert apart from link hrefs, which we guard. Themed with the teams CSS vars.
const markdownComponents: Components = {
  a: ({ href, children, ...props }) => {
    const safe = safeHref(href);
    return safe ? (
      <a href={safe} target="_blank" rel="noopener noreferrer"
         style={{ color: "var(--forest)", textDecoration: "underline" }} {...props}>
        {children}
      </a>
    ) : (
      <span {...props}>{children}</span>
    );
  },
  h1: ({ children }) => <h1 className="text-base font-semibold mt-3 mb-1">{children}</h1>,
  h2: ({ children }) => <h2 className="text-sm font-semibold mt-3 mb-1">{children}</h2>,
  h3: ({ children }) => <h3 className="text-sm font-semibold mt-2 mb-1">{children}</h3>,
  p: ({ children }) => <p className="text-sm my-1 leading-relaxed">{children}</p>,
  ul: ({ children }) => <ul className="list-disc pl-5 text-sm my-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 text-sm my-1">{children}</ol>,
  li: ({ children }) => <li className="my-0.5">{children}</li>,
  code: ({ className, children }) => {
    const isBlock = typeof className === "string" && className.includes("language-");
    return isBlock ? (
      <code className="font-mono">{children}</code>
    ) : (
      <code className="rounded px-1 py-0.5 text-xs font-mono"
            style={{ background: "var(--edge)" }}>{children}</code>
    );
  },
  pre: ({ children }) => (
    <pre className="rounded p-2 text-xs font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-sm"
                style={{ borderColor: "var(--edge)", color: "var(--mute)" }}>{children}</blockquote>
  ),
};

/** Shared read-only markdown renderer (gfm, no raw HTML, link-guarded, CSS-var themed). */
export function MarkdownView({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
      {children}
    </ReactMarkdown>
  );
}
