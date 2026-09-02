"use client";

// One vault page. Read mode renders the plugin's markdown as a wiki page —
// frontmatter hidden, managed-block comments skipped, [[wikilinks]] resolved
// against the tree and navigated IN-PANEL (href stays a real deep link so
// middle-click / new-tab work). Edit mode is the untouched FileContentViewer
// (raw text incl. frontmatter — the audit view — dirty tracking, scoped Cmd+S).
// Non-markdown and binary files always go to FileContentViewer (image
// preview / "binary" notice come free).

import * as React from "react";
import { Loader2 } from "lucide-react";
import type { Components } from "react-markdown";
import { FileContentViewer } from "@/components/chat/FileContentViewer";
import { MarkdownView, markdownComponents } from "@/components/teams/shared/components/MarkdownView";
import { PanelNotice } from "@/components/teams/shell";
import { buildPanelUrl } from "@/components/workspace/urlChannel";
import type { FileEntry, FileInfo } from "@/hooks/useWorkspaceFiles";
import { LANDING_PATH, resolveVaultLink, toReadableMarkdown } from "./brainVault";

export interface BrainPageProps {
  path: string;
  file: FileInfo | null;
  isLoading: boolean;
  error: Error | null;
  files: FileEntry[];
  editing: boolean;
  onNavigate: (path: string) => void;
  onRetry: () => void;
  onSave: (content: string) => Promise<void>;
  onDirtyChange: (dirty: boolean) => void;
}

// Shared anchor for everything that is NOT a vault link (external → new tab,
// attachment → authed download, Paperclip path → in-shell). Typed loosely:
// react-markdown's Components["a"] admits a string tag we never use.
const DefaultAnchor = markdownComponents.a as React.FC<React.ComponentPropsWithoutRef<"a">>;

export function BrainPage({
  path,
  file,
  isLoading,
  error,
  files,
  editing,
  onNavigate,
  onRetry,
  onSave,
  onDirtyChange,
}: BrainPageProps) {
  // Hoisted out of the JSX: an inline literal is a new object every render and
  // would defeat MarkdownView's own memo on `components`.
  const components = React.useMemo<Partial<Components>>(
    () => ({
      a: ({ href, children, ...rest }) => {
        const link = href ? resolveVaultLink(href, files) : null;
        if (!link)
          return (
            <DefaultAnchor href={href} {...rest}>
              {children}
            </DefaultAnchor>
          );
        return (
          <a
            href={buildPanelUrl("teams-brain", { path: link.path })}
            title={link.exists ? undefined : "No page here yet"}
            className={
              link.exists
                ? "underline text-[var(--midnight)]"
                : "underline decoration-dotted text-[var(--midnight-muted)]"
            }
            onClick={(e) => {
              // Modified / non-left clicks keep the native deep link.
              if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
              e.preventDefault();
              onNavigate(link.path);
            }}
          >
            {children}
          </a>
        );
      },
      // Reading-pane scale; the shared set is tuned for comment threads.
      h1: ({ children }) => (
        <h1 className="font-editorial-serif text-2xl leading-tight mt-1 mb-3">{children}</h1>
      ),
      h2: ({ children }) => <h2 className="font-editorial-serif text-lg mt-6 mb-2">{children}</h2>,
      h3: ({ children }) => <h3 className="text-base font-semibold mt-4 mb-1">{children}</h3>,
      p: ({ children }) => <p className="text-base my-2 leading-relaxed">{children}</p>,
      ul: ({ children }) => <ul className="list-disc pl-5 text-base my-2">{children}</ul>,
      ol: ({ children }) => <ol className="list-decimal pl-5 text-base my-2">{children}</ol>,
    }),
    [files, onNavigate],
  );

  if (error) {
    // ApiError carries `status`; duck-typed so this file never imports
    // @/lib/api (Clerk) into a unit test.
    const status = (error as { status?: number }).status;
    if (status === 404) {
      return (
        <PanelNotice
          state="zero"
          headline={`No page at ${path}.`}
          supporting="The link is stale — the page was renamed, filed elsewhere, or never written."
          action={{ label: "Open the index", onClick: () => onNavigate(LANDING_PATH) }}
        />
      );
    }
    return (
      <PanelNotice
        state="unavailable"
        headline="Couldn't load this page."
        supporting="Something went wrong reaching your company's memory."
        action={{ label: "Retry", onClick: onRetry }}
      />
    );
  }

  if (isLoading || !file) {
    return (
      <div className="flex h-full items-center justify-center text-[var(--midnight-muted)]">
        <Loader2 className="mr-2 h-5 w-5 animate-spin" />
        Loading page…
      </div>
    );
  }

  const isMarkdown = !file.binary && file.content != null && file.name.endsWith(".md");
  if (editing || !isMarkdown) {
    return (
      <FileContentViewer
        file={file}
        isLoading={false}
        error={null}
        onSave={onSave}
        onDirtyChange={onDirtyChange}
      />
    );
  }

  return (
    <article className="max-w-prose px-4 py-3 font-serif text-[var(--midnight)]">
      <MarkdownView skipHtml components={components}>
        {toReadableMarkdown(file.content ?? "")}
      </MarkdownView>
    </article>
  );
}
