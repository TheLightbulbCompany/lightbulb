// apps/frontend/src/components/teams/issues/IssueContinuationHandoff.tsx

// Ported from upstream Paperclip's IssueContinuationHandoff
// (paperclip/ui/src/components/IssueContinuationHandoff.tsx @ v2026.916.1-isol8.2)
// (MIT, (c) 2025 Paperclip AI). Markup, copy, expand/copy/focus behaviour verbatim.
//
// Adaptations vs upstream (only what had to change):
//   - @paperclipai/shared IssueDocument + ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY
//                                    -> components/teams/shared/types + the constant below
//   - MarkdownBody (+ externalReferences, softBreaks={false})
//                                    -> MarkdownView (shared editorial renderer)
//   - relativeTime                   -> shared/lib/timeAgo
//   - copyTextToClipboard            -> navigator.clipboard.writeText
//   - upstream CSS-var text utilities -> text-2xs (nano, 10px) / text-caption (micro, 11px)
//   - focusSignal (scroll-to + highlight) dropped: nothing on our page emits it

"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, Copy, History } from "lucide-react";
import type { IssueDocument } from "@/components/teams/shared/types";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { timeAgo } from "@/components/teams/shared/lib/timeAgo";

/** Upstream packages/shared/src/constants.ts ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY. */
export const ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY = "continuation-summary" as const;

type IssueContinuationHandoffProps = {
  document: IssueDocument | null | undefined;
};

export function IssueContinuationHandoff({ document }: IssueContinuationHandoffProps) {
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const copiedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (copiedTimerRef.current) {
        clearTimeout(copiedTimerRef.current);
      }
    };
  }, []);

  const copyBody = useCallback(async () => {
    if (!document) return;
    try {
      await navigator.clipboard.writeText(document.body ?? "");
    } catch {
      return;
    }
    setCopied(true);
    if (copiedTimerRef.current) {
      clearTimeout(copiedTimerRef.current);
    }
    copiedTimerRef.current = setTimeout(() => setCopied(false), 1500);
  }, [document]);

  if (!document) return null;

  const title = document.title?.trim() || "Continuation handoff";
  const revision = document.latestRevisionNumber ?? 0;

  return (
    <div
      id={`document-${ISSUE_CONTINUATION_SUMMARY_DOCUMENT_KEY}`}
      className="mb-3 rounded-lg border border-border bg-accent/20 p-3"
    >
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-sm text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          onClick={() => setExpanded((current) => !current)}
          aria-label={expanded ? "Collapse continuation handoff" : "Expand continuation handoff"}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        </button>
        <History className="h-4 w-4 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-ui font-medium text-foreground">{title}</span>
            <Badge variant="outline" className="border-border font-mono text-2xs uppercase text-muted-foreground">
              handoff
            </Badge>
          </div>
          <div className="text-caption text-muted-foreground">
            Updated {document.updatedAt ? timeAgo(document.updatedAt) : ""}
            {revision > 0 ? ` - revision ${revision}` : ""}
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={copyBody} className="shrink-0">
          {copied ? <Check className="mr-1.5 h-3.5 w-3.5" /> : <Copy className="mr-1.5 h-3.5 w-3.5" />}
          {copied ? "Copied" : "Copy"}
        </Button>
      </div>
      {expanded ? (
        <div className="mt-3 rounded-md border border-border bg-background/80 p-3 text-ui leading-6">
          <MarkdownView>{document.body ?? ""}</MarkdownView>
        </div>
      ) : null}
    </div>
  );
}
