"use client";

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Components } from "react-markdown";
import { ChevronRight, ChevronDown } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";

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
  code: ({ children }) => (
    <code className="rounded px-1 py-0.5 text-xs font-mono"
          style={{ background: "var(--edge)" }}>{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="rounded p-2 text-xs font-mono overflow-x-auto my-2"
         style={{ background: "var(--edge)" }}>{children}</pre>
  ),
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 pl-3 my-2 text-sm"
                style={{ borderColor: "var(--edge)", color: "var(--mute)" }}>{children}</blockquote>
  ),
};

export function DocumentRow({ doc, agents }: { doc: IssueDocument; agents: CompanyAgent[] }) {
  const [open, setOpen] = useState(false);
  const when = doc.updatedAt ?? doc.createdAt;
  const titleShown =
    doc.title && doc.title.trim() && doc.title.trim() !== doc.key.trim()
      ? doc.title.trim()
      : null;
  const agentName = doc.updatedByAgentId
    ? agents.find((a) => a.id === doc.updatedByAgentId)?.name ?? null
    : null;
  const Chevron = open ? ChevronDown : ChevronRight;

  return (
    <li className="flex flex-col" style={{ border: "1px solid var(--edge)", borderRadius: 6 }}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-2 px-3 py-2 text-left"
      >
        <Chevron size={14} className="flex-shrink-0" style={{ color: "var(--mute)" }} />
        <span
          className="text-[10px] uppercase tracking-wide font-mono px-1.5 py-0.5 rounded flex-shrink-0"
          style={{ background: "var(--edge)", color: "var(--ink)" }}
        >
          {doc.key}
        </span>
        {titleShown ? (
          <span className="flex-1 truncate text-[var(--ink)]">{titleShown}</span>
        ) : (
          <span className="flex-1" />
        )}
        {when && (
          <span className="text-xs text-[var(--mute)] flex-shrink-0">
            updated {timeAgo(when)}
            {agentName ? ` · ${agentName}` : ""}
          </span>
        )}
      </button>
      {open && (
        <div className="px-3 pb-3 pt-1 max-w-none text-[var(--ink)]"
             style={{ borderTop: "1px solid var(--edge)" }}>
          {doc.body && doc.body.trim() ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>
              {doc.body}
            </ReactMarkdown>
          ) : (
            <p className="text-xs text-[var(--mute)] pt-2">(empty document)</p>
          )}
        </div>
      )}
    </li>
  );
}
