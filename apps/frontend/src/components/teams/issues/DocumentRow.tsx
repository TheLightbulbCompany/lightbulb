"use client";

import { useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import type { CompanyAgent, IssueDocument } from "@/components/teams/shared/types";

export function DocumentRow({ doc, agents }: { doc: IssueDocument; agents: CompanyAgent[] }) {
  const [open, setOpen] = useState(false);
  const when = doc.updatedAt ?? doc.createdAt;
  const titleShown =
    doc.title && doc.title.trim() && doc.title.trim().toLowerCase() !== doc.key.trim().toLowerCase()
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
            <MarkdownView>{doc.body}</MarkdownView>
          ) : (
            <p className="text-xs text-[var(--mute)] pt-2">(empty document)</p>
          )}
        </div>
      )}
    </li>
  );
}
