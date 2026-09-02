"use client";

// One vault page. Read mode renders the plugin's markdown as a wiki page —
// managed-block comments skipped, [[wikilinks]] resolved against the tree and
// navigated IN-PANEL (href stays a real deep link so middle-click / new-tab
// work), the YAML head lifted out as pills and the trailing `## Related` list
// as a chip block. Edit mode is the untouched FileContentViewer
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
import {
  LANDING_PATH,
  pagePills,
  parseFrontmatter,
  resolveVaultLink,
  splitRelated,
  toReadableMarkdown,
} from "./brainVault";

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

const PILL =
  "inline-flex items-center rounded-full border border-[var(--edge)] " +
  "bg-[var(--cream-deep)] px-2.5 py-1 label-caps text-[var(--midnight-soft)]";

/** One in-vault navigation. The href stays a real deep link so middle-click
 *  and "open in new tab" work; a plain left-click is intercepted and handled
 *  in-panel. Used by both the markdown `a` override and the Related chips. */
function VaultLink({
  link,
  onNavigate,
  className,
  children,
}: {
  link: { path: string; exists: boolean };
  onNavigate: (path: string) => void;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <a
      href={buildPanelUrl("teams-brain", { path: link.path })}
      title={link.exists ? undefined : "No page here yet"}
      className={
        className ??
        (link.exists
          ? "underline text-[var(--midnight)]"
          : "underline decoration-dotted text-[var(--midnight-muted)]")
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
}

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
          <VaultLink link={link} onNavigate={onNavigate}>
            {children}
          </VaultLink>
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

  // The page's own metadata, lifted out of the body: YAML head → pills, the
  // trailing `## Related` list → a block. Both are page-level facts, and the
  // breadcrumb directly above already names the page, so the pills sit under
  // that title rather than being threaded between the body's h1 and its first
  // paragraph — which would need string surgery on markdown the plugin owns,
  // and would silently drop the metadata on any page with no h1.
  const { meta, body } = parseFrontmatter(file.content ?? "");
  const { body: prose, related } = splitRelated(body);
  const pills = pagePills(meta);
  const readable = toReadableMarkdown(prose).trim();

  // The plugin's stub shape is frontmatter and nothing else, so a real page can
  // read as "" once the head is lifted off — an empty <article> would be
  // indistinguishable from a broken panel. Pills alone are not content: they
  // describe a page that says nothing. A compiled Related list IS content —
  // it is the only thing on a freshly linked stub, and it navigates.
  if (!readable && related.length === 0) {
    return (
      <PanelNotice
        state="zero"
        headline="This page is empty."
        supporting="Nothing has been written here yet."
      />
    );
  }

  return (
    <article className="max-w-prose px-4 py-3 font-serif text-[var(--midnight)]">
      {pills.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {pills.map((p) => (
            <span key={p} className={PILL}>
              {p}
            </span>
          ))}
        </div>
      )}
      <MarkdownView skipHtml components={components}>
        {readable}
      </MarkdownView>
      {related.length > 0 && (
        <section className="mt-6 border-t border-[var(--edge)] pt-3">
          <p className="label-caps mb-2">Related</p>
          <div className="flex flex-wrap gap-1.5">
            {related.map((r) => {
              const link = resolveVaultLink(r.target, files);
              return link ? (
                <VaultLink key={r.target} link={link} onNavigate={onNavigate} className={PILL}>
                  {r.alias}
                </VaultLink>
              ) : null;
            })}
          </div>
        </section>
      )}
    </article>
  );
}
